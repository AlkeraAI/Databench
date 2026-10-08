"""Cell references and graph targets, resolved before a call is gated.

A tool input may name a cell the way a person does (``last``, ``Cell 3``, a
name) and may ask for a run by the dependency graph (``upstream`` or
``downstream`` of a cell). :func:`resolve_args` turns both into what the port
takes, cell ids and the engine's targets, once, in
:func:`~alkera_notebook.tools.catalog.call_tool`, before the gate is asked.
So the plan a person approves is the plan that runs, and no port or function
sees a reference it would have to read again.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from alkera_notebook.cell_names import CellRefError, resolve_cell_ref
from alkera_notebook.tools.models import (
    EngineRunTarget,
    NotebookCellsInput,
    NotebookGraphInput,
    NotebookOutputInput,
    NotebookReadInput,
    NotebookRunInput,
    NotebookShowOutputInput,
    RunAbove,
    RunAll,
    RunBelow,
    RunCells,
    RunDownstream,
    RunStale,
    RunTarget,
    RunUpstream,
)
from alkera_notebook.tools.port import NotebookPort, NotebookToolError

#: The inputs that name cells. Edit ops are not among them: an op addresses
#: a cell by id, because a batch may create and address cells in one go.
_REFERRING = (
    NotebookReadInput,
    NotebookRunInput,
    NotebookCellsInput,
    NotebookOutputInput,
    NotebookShowOutputInput,
    NotebookGraphInput,
)


async def _cells(port: NotebookPort) -> list[tuple[str, str]]:
    view = await port.read(None, include_source=False, include_outputs=False)
    return [(cell.id, cell.name) for cell in view.cells]


def _one(ref: str, cells: Sequence[tuple[str, str]]) -> str:
    try:
        return resolve_cell_ref(ref, cells)
    except CellRefError as exc:
        raise NotebookToolError(exc.code, str(exc)) from exc


async def _target(
    target: RunTarget, port: NotebookPort, cells: Sequence[tuple[str, str]]
) -> EngineRunTarget:
    if isinstance(target, RunCells):
        return RunCells(ids=list(dict.fromkeys(_one(ref, cells) for ref in target.ids)))
    if isinstance(target, RunAbove):
        return RunAbove(id=_one(target.id, cells))
    if isinstance(target, RunBelow):
        return RunBelow(id=_one(target.id, cells))
    if isinstance(target, RunUpstream | RunDownstream):
        cid = _one(target.id, cells)
        upstream = isinstance(target, RunUpstream)
        graph = await port.graph(cid, "up" if upstream else "down")
        related = set(graph.upstream if upstream else graph.downstream) | {cid}
        return RunCells(ids=[c for c, _ in cells if c in related])
    assert isinstance(target, RunAll | RunStale)
    return target


async def resolve_args(args: BaseModel, port: NotebookPort) -> BaseModel:
    """``args`` with every cell reference an id and every run target one the
    engine plans. Raises :class:`NotebookToolError` (``cell_not_found``,
    ``ambiguous_cell``) for a reference that names no single cell."""
    if not isinstance(args, _REFERRING):
        return args
    cells = await _cells(port)
    if isinstance(args, NotebookRunInput):
        return args.model_copy(update={"target": await _target(args.target, port, cells)})
    if isinstance(args, NotebookReadInput | NotebookCellsInput):
        if args.cells is None:
            return args
        return args.model_copy(update={"cells": [_one(ref, cells) for ref in args.cells]})
    if args.cell is None:
        return args
    return args.model_copy(update={"cell": _one(args.cell, cells)})


def engine_target(target: RunTarget) -> EngineRunTarget:
    """``target`` as the port takes it; a graph target must have been resolved
    by :func:`resolve_args` first."""
    if isinstance(target, RunUpstream | RunDownstream):
        raise NotebookToolError(
            "unresolved_target", f"A {target.kind} target reached the engine unresolved."
        )
    return target


__all__ = ["engine_target", "resolve_args"]
