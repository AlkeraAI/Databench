"""FileDocumentStore behaviour outside the case catalogue."""

from __future__ import annotations

from pathlib import Path

import pytest
from alkera_notebook.document import file_store
from alkera_notebook.document.apply import apply_ops
from alkera_notebook.document.file_store import FileDocumentStore
from alkera_notebook.document.fmt import ModuleFormat
from alkera_notebook.document.model import Document
from alkera_notebook.document.ops import (
    InsertCell,
    MoveCell,
    ReplaceCell,
    SetSetting,
)
from alkera_notebook.engine.errors import ForbiddenError, NotFoundError
from alkera_notebook.engine.models import Actor

FMT = ModuleFormat()
ANN = Actor(kind="person", id="ann", display_name="Ann", can_edit=True, can_run=True)
VIEWER = ANN.model_copy(update={"can_edit": False})
PATH = "nb.alknb.py"
BROKEN_CELL_NOTEBOOK = (
    'import marimo\n\n__generated_with = "0.25.1"\napp = marimo.App()\n\n\n'
    '@app.cell(alkera_id="a1b2c3d4e5")\ndef _():\n    x = (\n    return\n'
)


async def seeded(tmp_path: Path) -> tuple[FileDocumentStore, list[str]]:
    store = FileDocumentStore(tmp_path, fmt=FMT)
    stored = await store.create(
        PATH, [InsertCell(source="x = 1"), InsertCell(source="y = x")], {}, ANN
    )
    return store, list(stored.document.order)


async def test_nbeng_doc_create_from_document_and_settings(tmp_path: Path) -> None:
    doc = apply_ops(Document(), [InsertCell(source="x = 1")], fmt=FMT, new_id=FMT.new_cell_id)
    store = FileDocumentStore(tmp_path, fmt=FMT)
    stored = await store.create(PATH, doc.document, {"reactivity": "lazy"}, ANN)
    assert stored.document.order == doc.document.order
    assert '# reactivity = "lazy"' in (tmp_path / PATH).read_text()
    with pytest.raises(ForbiddenError):
        await store.create("other.alknb.py", [], {}, VIEWER)


async def test_nbeng_doc_load_missing_and_invalid(tmp_path: Path) -> None:
    store = FileDocumentStore(tmp_path, fmt=FMT)
    with pytest.raises(NotFoundError):
        await store.load("absent.alknb.py")
    # A notebook whose cell does not parse: the cell is kept, and the file is
    # saved beside the notebook before the first write replaces it.
    bad = BROKEN_CELL_NOTEBOOK
    (tmp_path / "bad.alknb.py").write_text(bad)
    stored = await store.load("bad.alknb.py")
    assert [n.kind for n in stored.notices] == ["invalid_file"]
    assert await store.editing("never-loaded.alknb.py") == {}
    r = await store.apply("bad.alknb.py", [InsertCell(source="v = 1")], None, ANN, None)
    saved = tmp_path / r.notices[0].data["saved_as"]
    assert saved.read_text() == bad


async def test_nbeng_doc_change_queue_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(file_store, "CHANGE_QUEUE_MAX", 3)
    store, ids = await seeded(tmp_path)
    stream = store.changes(PATH)
    tokens = []
    for n in range(6):
        r = await store.apply(
            PATH, [ReplaceCell(cell_id=ids[0], source=f"x = {n}")], None, ANN, None
        )
        tokens.append(r.token)
    got = [(await stream.__anext__()).token for _ in range(3)]
    # A subscriber that did not read keeps only the newest changes.
    assert got == tokens[-3:]
    await stream.aclose()


async def test_nbeng_doc_submit_memory_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(file_store, "SUBMIT_MEMORY", 2)
    store, _ = await seeded(tmp_path)
    for n in range(3):
        await store.apply(PATH, [InsertCell(source="v = 0")], None, ANN, f"submit-{n:04d}")
    again = await store.apply(PATH, [InsertCell(source="v = 0")], None, ANN, "submit-0000")
    assert not again.repeat
    kept = await store.apply(PATH, [InsertCell(source="v = 0")], None, ANN, "submit-0002")
    assert kept.repeat


async def test_nbeng_doc_batch_reorder_merges_outside_addition(tmp_path: Path) -> None:
    store, ids = await seeded(tmp_path)
    f = tmp_path / PATH
    text = f.read_text().replace(
        "if __name__",
        '@app.cell(alkera_id="q0q0q0q0q0")\ndef _():\n    n = 5\n    return\n\n\nif __name__',
    )
    f.write_text(text)
    await store.apply(PATH, [MoveCell(cell_id=ids[0])], None, ANN, None)
    order = (await store.load(PATH)).document.order
    # The batch's order wins; the outside cell follows its neighbour in the file.
    assert order == [ids[1], "q0q0q0q0q0", ids[0]]
    assert "n = 5" in f.read_text()


async def test_nbeng_doc_settings_merge(tmp_path: Path) -> None:
    store, ids = await seeded(tmp_path)
    f = tmp_path / PATH
    f.write_text(f.read_text().replace("# <<< alkera", '# reactivity = "lazy"\n# <<< alkera'))
    r = await store.apply(PATH, [ReplaceCell(cell_id=ids[0], source="x = 2")], None, ANN, None)
    assert r.notices == []
    assert (await store.load(PATH)).document.settings["reactivity"] == "lazy"
    # Both sides change the same setting differently: memory wins, file kept beside.
    f.write_text(
        f.read_text().replace('"lazy"', '"autorun"').replace('# reactivity = "autorun"\n', "")
    )
    f.write_text(f.read_text().replace("# <<< alkera", '# dataframe = "pandas"\n# <<< alkera'))
    r = await store.apply(PATH, [SetSetting(key="dataframe", value="polars")], None, ANN, None)
    assert [n.kind for n in r.notices] == ["external_conflict"]
    assert r.notices[0].cell_id is None
    assert (await store.load(PATH)).document.settings["dataframe"] == "polars"
