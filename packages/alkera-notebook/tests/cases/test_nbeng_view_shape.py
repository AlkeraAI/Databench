"""The engine's view and operations result: the one definition of
``NotebookView``, ``CellAfterOp`` and ``GraphSummary`` that the agent tools
and the platform's notebook route share. No kernel is started."""

from __future__ import annotations

from pathlib import Path

from alkera_notebook.document.ops import DeleteCell, InsertCell, ReplaceCell
from nbeng_harness import BOB, engine_for, notebook


async def test_the_view_carries_each_cell_s_document_settings_and_who_is_where(
    tmp_path: Path,
) -> None:
    """A reader that rebuilds the document from the view (the box's store)
    gets each cell's config, meta and extra; presence says who, as what
    kind of actor, and when."""
    async with engine_for(tmp_path) as engine:
        sql = InsertCell(
            kind="sql",
            source="SELECT 1 AS v",
            meta={"output_var": "orders", "connection": "local"},
            config={"hide_code": True},
        )
        session, ann, (a, b) = await notebook(engine, ["x = 1", sql])
        bob = session.attach(BOB)
        await bob.apply([ReplaceCell(cell_id=a, source="x = 2")], None)
        view = await ann.read()
        by_id = {cell.id: cell for cell in view.cells}
        assert {k: by_id[b].meta[k] for k in ("output_var", "connection")} == {
            "output_var": "orders",
            "connection": "local",
        }
        assert by_id[b].config == {"hide_code": True}
        assert (by_id[a].meta, by_id[a].config) == ({}, {})
        (present,) = view.presence
        assert (present.who, present.cell_id, present.kind) == ("Bob", a, "person")
        assert present.at is not None


async def test_an_ops_result_carries_run_statuses_and_structured_graph_errors(
    tmp_path: Path,
) -> None:
    """The store holds no run state, so the engine fills each live cell's
    status; a deleted cell has no index and no status; a duplicated
    definition is one error object naming the name and both cells."""
    async with engine_for(tmp_path) as engine:
        _session, ann, (a, b) = await notebook(engine, ["x = 1", "y = x"])
        result = await ann.apply([InsertCell(source="x = 2", after=b), DeleteCell(cell_id=b)], None)
        (new,) = result.created
        by_id = {cell.id: cell for cell in result.cells}
        assert (by_id[new].status, by_id[new].deleted) == ("not_run", False)
        assert (by_id[b].deleted, by_id[b].index, by_id[b].status) == (True, None, None)
        assert result.graph.computed is True
        expected = {"code": "multiple_definitions", "name": "x", "cells": sorted([a, new])}
        assert [e.model_dump() for e in result.graph.cells[a].errors] == [expected]
        assert [e.model_dump() for e in result.graph.cells[new].errors] == [expected]
        assert b not in result.graph.cells
