"""A bounded memo in front of marimo's ``compile_cell``.

Compiling every cell is most of what writing and analyzing a notebook costs,
and both run on every edit while only the edited cell's code changed. The memo
is keyed by everything a compile reads: the code, the cell id (marimo mangles
cell-local names with it), and the process state the visitor consults (the
calls it reads as SQL, and whether DuckDB and sqlglot are importable).

The memo never hands out what it holds: each call returns a new ``CellImpl``
with its own containers, configuration and runtime state, because marimo's
serializer configures the cell it is given in place.
"""

from __future__ import annotations

import copy
import dataclasses
import functools
from collections.abc import Callable, Hashable, Sequence
from typing import Any

from alkera_notebook.format import _fork
from alkera_notebook.format.ids import is_cell_id

#: How many distinct ``(code, cell id)`` compiles the memo keeps.
COMPILE_CACHE_SIZE = 4096


@dataclasses.dataclass(frozen=True, slots=True)
class _Unparsable:
    """A compile that raised ``SyntaxError``, remembered as one."""

    args: tuple[Any, ...]


def compile_environment() -> Hashable:
    """What a compile reads besides the code and the id. Taken once per
    notebook (each check looks for an installed package), not per cell."""
    duckdb = _fork.DependencyManager.duckdb
    sqlglot = _fork.DependencyManager.sqlglot
    try:
        return (
            tuple(_fork.SQL_CALLS),
            duckdb.has_at_version(min_version="1.0.0", quiet=True),
            sqlglot.has(quiet=True),
        )
    except Exception:
        # Unknown: a token equal to no other, so nothing is reused under it.
        return object()


@functools.lru_cache(maxsize=COMPILE_CACHE_SIZE)
def _compiled(code: str, cell_id: str, environment: Hashable) -> Any:
    try:
        return _fork.compile_cell(code, cell_id=_fork.CellId(cell_id))
    except SyntaxError as exc:
        return _Unparsable(exc.args)


def _fresh(cell: Any) -> Any:
    """``cell`` with every mutable part of its own. The AST, the code
    objects and the per-variable records stay shared: nothing that reads a
    compile for serialization or the graph writes to them."""
    return dataclasses.replace(
        cell,
        defs=set(cell.defs),
        refs=set(cell.refs),
        sql_refs=dict(cell.sql_refs),
        temporaries=set(cell.temporaries),
        variable_data={name: list(found) for name, found in cell.variable_data.items()},
        deleted_refs=set(cell.deleted_refs),
        closed_over_temporaries=set(cell.closed_over_temporaries),
        config=copy.deepcopy(cell.config),
        import_workspace=_fork.ImportWorkspace(),
        _status=_fork.RuntimeState(),
        _run_result_status=_fork.RunResultStatus(),
        _stale=_fork.CellStaleState(),
        _output=_fork.CellOutput(),
    )


def compile_cell(code: str, cell_id: str, environment: Hashable | None = None) -> Any:
    """``compile_cell(code, cell_id)``, from the memo when it can be. Raises
    ``SyntaxError`` as the compile did; any other failure is not memoized."""
    env = compile_environment() if environment is None else environment
    found = _compiled(code, str(cell_id), env)
    if isinstance(found, _Unparsable):
        raise SyntaxError(*found.args)
    return _fresh(found)


def cell_compiler(ids: Sequence[str] = ()) -> Callable[..., Any]:
    """A compiler for marimo's top-level extraction, with the environment
    taken once for the notebook it serializes.

    The extraction names cells by position (``"0"``, ``"1"``, ...); ``ids``
    holds each position's own cell id, and a cell is compiled under that id
    instead, so inserting, deleting or moving a cell leaves every other
    cell's compile reusable. The id is part of what a compile is (marimo
    mangles a cell's private names with it), so it is never swapped on a
    cached result; what the extraction writes reads no mangled name, so the
    file is the same whichever id a cell was compiled under. A position
    without a valid cell id is compiled by position, as marimo does.
    """
    environment = compile_environment()

    def compile_(code: str, cell_id: str) -> Any:
        position = str(cell_id)
        own = ids[int(position)] if position.isdecimal() and int(position) < len(ids) else None
        return compile_cell(code, own if own and is_cell_id(own) else position, environment)

    return compile_


def cache_info() -> functools._CacheInfo:
    """The memo's hits, misses and size."""
    return _compiled.cache_info()


def cache_clear() -> None:
    _compiled.cache_clear()
