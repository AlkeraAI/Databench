"""Every op that names a cell refuses a missing one with ``cell_not_found``,
naming the op's index in the batch, and applies nothing.

The cell an op acts on and every anchor it names (``after``, ``before``) are
checked, including anchors the op would not end up using: an insert given
both anchors, a restore of a cell that is not deleted, a move onto itself.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from alkera_notebook.document.file_store import FileDocumentStore
from alkera_notebook.document.fmt import ModuleFormat
from alkera_notebook.document.ops import (
    DeleteCell,
    EditCell,
    InsertCell,
    MoveCell,
    NotebookOp,
    NotebookOpError,
    ReplaceCell,
    RestoreCell,
    SetCellConfig,
    SetCellKind,
    SetCellName,
    TextEdit,
)
from alkera_notebook.engine import Actor
from alkera_notebook.events.models import DocChanged
from alkera_notebook.tools import (
    ActorRef,
    NotebookToolError,
    RecordingGatekeeper,
    call_tool,
    validate,
)
from alkera_notebook.tools.engine_adapter import LocalEngines
from nbeng_harness import BOB, engine_for, notebook

ANN = Actor(kind="person", id="ann", display_name="Ann", can_edit=True, can_run=True)
GONE = "zzzzzzzzzz"
PATH = "nb.alknb.py"

# Each case builds the op from (live cell id, deleted cell id).
Case = Callable[[str, str], NotebookOp]

CASES: list[object] = [
    pytest.param(
        lambda live, dead: EditCell(cell_id=GONE, edits=[TextEdit(old="x", new="y")]), id="edit"
    ),
    pytest.param(lambda live, dead: ReplaceCell(cell_id=GONE, source="y = 2"), id="replace"),
    pytest.param(lambda live, dead: DeleteCell(cell_id=GONE), id="delete"),
    pytest.param(lambda live, dead: RestoreCell(cell_id=GONE), id="restore"),
    pytest.param(
        lambda live, dead: RestoreCell(cell_id=dead, after=GONE), id="restore_deleted_after"
    ),
    pytest.param(lambda live, dead: RestoreCell(cell_id=live, after=GONE), id="restore_live_after"),
    pytest.param(lambda live, dead: MoveCell(cell_id=GONE), id="move"),
    pytest.param(lambda live, dead: MoveCell(cell_id=live, after=GONE), id="move_after"),
    pytest.param(lambda live, dead: MoveCell(cell_id=live, before=GONE), id="move_before"),
    pytest.param(
        lambda live, dead: MoveCell(cell_id=live, after=live, before=GONE), id="move_onto_itself"
    ),
    pytest.param(lambda live, dead: SetCellName(cell_id=GONE, name="n"), id="rename"),
    pytest.param(lambda live, dead: SetCellKind(cell_id=GONE, kind="markdown"), id="set_kind"),
    pytest.param(
        lambda live, dead: SetCellConfig(cell_id=GONE, config={"hide_code": True}), id="set_config"
    ),
    pytest.param(lambda live, dead: InsertCell(after=GONE), id="insert_after"),
    pytest.param(lambda live, dead: InsertCell(before=GONE), id="insert_before"),
    pytest.param(
        lambda live, dead: InsertCell(after=live, before=GONE), id="insert_with_both_anchors"
    ),
]


async def _seeded(tmp_path: Path) -> tuple[FileDocumentStore, str, str]:
    store = FileDocumentStore(tmp_path, fmt=ModuleFormat())
    stored = await store.create(
        PATH, [InsertCell(source="x = 1"), InsertCell(source="y = x")], {}, ANN
    )
    live, dead = stored.document.order
    await store.apply(PATH, [DeleteCell(cell_id=dead)], None, ANN, None)
    return store, live, dead


@pytest.mark.parametrize("make", CASES)
async def test_store_refuses_a_missing_cell_naming_the_op(tmp_path: Path, make: Case) -> None:
    store, live, dead = await _seeded(tmp_path)
    before_text = (tmp_path / PATH).read_text()
    before_token = (await store.load(PATH)).token
    with pytest.raises(NotebookOpError) as info:
        await store.apply(PATH, [InsertCell(source="v = 0"), make(live, dead)], None, ANN, None)
    assert (info.value.code, info.value.index) == ("cell_not_found", 1)
    assert info.value.to_dict()["index"] == 1
    assert (tmp_path / PATH).read_text() == before_text
    assert (await store.load(PATH)).token == before_token


@pytest.mark.parametrize("index", [0, 2])
async def test_the_index_is_the_failing_ops_own(tmp_path: Path, index: int) -> None:
    store, live, _dead = await _seeded(tmp_path)
    ops: list[NotebookOp] = [InsertCell(source="v = 0"), SetCellName(cell_id=live, name="a")]
    ops.insert(index, DeleteCell(cell_id=GONE))
    with pytest.raises(NotebookOpError) as info:
        await store.apply(PATH, ops, None, ANN, None)
    assert (info.value.code, info.value.index) == ("cell_not_found", index)


@pytest.mark.parametrize(
    "op",
    [
        pytest.param({"op": "delete", "cell_id": GONE}, id="delete"),
        pytest.param({"op": "restore", "cell_id": "LIVE", "after": GONE}, id="restore_live_after"),
        pytest.param({"op": "insert", "after": "LIVE", "before": GONE}, id="insert_both"),
    ],
)
async def test_agent_tool_gets_the_structured_error_through_the_engine(
    tmp_path: Path, op: dict[str, object]
) -> None:
    engines = LocalEngines()
    agent = ActorRef(kind="agent", id="agent:t", display_name="Agent")
    host = engines(tmp_path, agent)
    gate = RecordingGatekeeper()
    try:
        made = await call_tool(
            "notebook.create",
            validate("notebook.create", {"path": PATH, "cells": [{"source": "x = 1"}]}),
            host=host,
            gatekeeper=gate,
        )
        live = made.cells[0].id  # type: ignore[attr-defined]
        named = {k: (live if v == "LIVE" else v) for k, v in op.items()}
        args = validate("notebook.edit", {"path": PATH, "ops": [{"op": "insert"}, named]})
        with pytest.raises(NotebookToolError) as info:
            await call_tool("notebook.edit", args, host=host, gatekeeper=gate)
        assert (info.value.code, info.value.op_index) == ("cell_not_found", 1)
        assert (tmp_path / PATH).read_text().count("@app.cell") == 1
    finally:
        await engines.close()


@pytest.mark.parametrize(
    ("ops", "index"),
    [
        pytest.param([DeleteCell(cell_id=GONE)], 0, id="alone"),
        pytest.param(
            [InsertCell(source="v = 0"), DeleteCell(cell_id=GONE)], 1, id="after_an_insert"
        ),
    ],
)
async def test_engine_client_refuses_deleting_a_missing_cell(
    tmp_path: Path, ops: list[NotebookOp], index: int
) -> None:
    async with engine_for(tmp_path) as engine:
        session, client, ids = await notebook(engine, ["x = 1", "y = x"])
        watcher = session.attach(BOB)
        while len(watcher.queue):
            await watcher.next_event(1)
        before = await client.read()
        with pytest.raises(NotebookOpError) as info:
            await client.apply(ops, None)
        assert (info.value.code, info.value.index) == ("cell_not_found", index)
        after = await client.read()
        assert (after.token, [c.id for c in after.cells]) == (before.token, ids)
        events = []
        while len(watcher.queue):
            events.append(await watcher.next_event(1))
        assert not [e for e in events if isinstance(e, DocChanged)]
