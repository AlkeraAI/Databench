"""Static analysis: each cell's definitions and references, and the edges between cells.

The analysis is marimo's (the fork's compiler and visitor), so the graph a
notebook gets here is the one marimo would build: ``alkera.sql`` and ``mo.sql``
queries contribute the tables they name, a SQL query's table counts as a
reference to a Python name only when another cell defines that name, and
cell-local ``_private`` names never leave their cell.

The runtime module (:data:`~alkera_notebook.format.templates.RUNTIME_MODULE`)
is provided the way a builtin is: the kernel binds it before any cell runs,
so a cell that uses it without importing it has no missing reference, and a
cell that does bind it (the setup block's import, or any other assignment) is
its definer under the usual rules: one definer, or ``multiple_definitions``.
"""

from __future__ import annotations

import ast
import hashlib
import io
import tokenize
from collections.abc import Iterable, Sequence
from typing import Any

from alkera_notebook.format import _compile, _fork
from alkera_notebook.format.ir import CellGraph, GraphError, GraphJSON, NotebookIR
from alkera_notebook.format.mangle import MangleError, mangle_source
from alkera_notebook.format.templates import RUNTIME_MODULE

#: Names no cell has to define: Python's builtins, and the runtime module the
#: kernel binds in every notebook's namespace.
_PROVIDED: frozenset[str] = frozenset({*_fork.BUILTINS, RUNTIME_MODULE})


def _error(code: str, name: str | None = None, cells: Iterable[str] = ()) -> GraphError:
    error: GraphError = {"code": code}
    if name is not None:
        error["name"] = name
    members = sorted(set(cells))
    if members:
        error["cells"] = members
    return error


def _sorted_errors(errors: Iterable[GraphError]) -> list[GraphError]:
    return sorted(errors, key=lambda e: (e["code"], e.get("name", ""), e.get("cells", [])))


def _empty(errors: Iterable[GraphError] = ()) -> CellGraph:
    return {"defs": [], "refs": [], "sql_tables": [], "errors": _sorted_errors(errors)}


def _strongly_connected(nodes: list[str], edges: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan's algorithm, iterative (cells can be many)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[list[str]] = []
    counter = 0
    for root in nodes:
        if root in index:
            continue
        work: list[tuple[str, Iterable[str]]] = [(root, iter(sorted(edges.get(root, ()))))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, successors = work[-1]
            advanced = False
            for succ in successors:
                if succ not in index:
                    index[succ] = low[succ] = counter
                    counter += 1
                    stack.append(succ)
                    on_stack.add(succ)
                    work.append((succ, iter(sorted(edges.get(succ, ())))))
                    advanced = True
                    break
                if succ in on_stack:
                    low[node] = min(low[node], index[succ])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(component)
    return components


def analyze_code(cells: Sequence[tuple[str, str]]) -> GraphJSON:
    """The graph of ``(id, code)`` pairs, in notebook order. Never raises.

    Used for the kernel graph (the code each cell was submitted with) as well
    as for a file's document graph (:func:`analyze`).
    """
    infos: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    environment = _compile.compile_environment()
    for cell_id, code in cells:
        if cell_id in infos:
            continue
        order.append(cell_id)
        try:
            impl = _compile.compile_cell(code, cell_id, environment)
        except (SyntaxError, ValueError, MemoryError, RecursionError):
            infos[cell_id] = {"syntax": True}
            continue
        except Exception:
            infos[cell_id] = {"syntax": True}
            continue
        sql = {str(name) for name in impl.sql_refs}
        infos[cell_id] = {
            "syntax": False,
            "defs": {str(name) for name in impl.defs},
            "refs": {str(name) for name in impl.refs},
            "sql": sql,
            "deleted": {str(name) for name in impl.deleted_refs},
        }

    definers: dict[str, set[str]] = {}
    for cell_id in order:
        info = infos[cell_id]
        if info["syntax"]:
            continue
        for name in info["defs"]:
            definers.setdefault(name, set()).add(cell_id)

    graph: dict[str, CellGraph] = {}
    edges: dict[str, set[str]] = {}
    for cell_id in order:
        info = infos[cell_id]
        if info["syntax"]:
            graph[cell_id] = _empty([_error("syntax_error")])
            continue
        errors: list[GraphError] = []
        refs: set[str] = set()
        for name in info["refs"]:
            others = definers.get(name, set()) - {cell_id}
            if name in info["sql"] and not others:
                continue  # a table no cell defines is not a Python reference
            if name in _PROVIDED and not others:
                continue
            refs.add(name)
            for other in others:
                edges.setdefault(other, set()).add(cell_id)
        for name in info["defs"]:
            if len(definers.get(name, ())) > 1:
                errors.append(_error("multiple_definitions", name, definers[name]))
        for name in info["deleted"]:
            if definers.get(name, set()) - {cell_id}:
                errors.append(_error("delete_nonlocal", name, definers[name] - {cell_id}))
        graph[cell_id] = {
            "defs": sorted(info["defs"]),
            "refs": sorted(refs),
            "sql_tables": sorted(info["sql"]),
            "errors": _sorted_errors(errors),
        }

    for component in _strongly_connected(order, edges):
        if len(component) > 1:
            for member in component:
                graph[member]["errors"] = _sorted_errors(
                    [*graph[member]["errors"], _error("cycle", None, component)]
                )

    edge_list = sorted([a, b] for a, targets in edges.items() for b in targets)
    return {"cells": graph, "edges": edge_list}


def analyze(ir: NotebookIR) -> GraphJSON:
    """The document graph of a notebook. Never raises.

    ``unparsable`` cells take no part in the graph (marimo does not run them);
    they carry ``syntax_error`` when their code does not compile.
    """
    runnable: list[tuple[str, str]] = []
    kept: dict[str, CellGraph] = {}
    for cell in ir.cells:
        if cell.kind == "unparsable":
            kept[cell.id] = _empty([] if _compiles(cell.code) else [_error("syntax_error")])
        else:
            runnable.append((cell.id, cell.code))
    result = analyze_code(runnable)
    cells = dict(result["cells"])
    cells.update({k: v for k, v in kept.items() if k not in cells})
    ordered = {cell.id: cells[cell.id] for cell in ir.cells if cell.id in cells}
    return {"cells": ordered, "edges": result["edges"]}


def _compiles(code: str) -> bool:
    try:
        ast.parse(code)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return False
    return True


def _ends_with_semicolon(code: str) -> bool:
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(code).readline))
    except (tokenize.TokenError, SyntaxError):
        return code.rstrip().endswith(";")
    for token in reversed(tokens):
        if token.type in (
            tokenize.ENDMARKER,
            tokenize.NEWLINE,
            tokenize.NL,
            tokenize.COMMENT,
            tokenize.INDENT,
            tokenize.DEDENT,
        ):
            continue
        return token.type == tokenize.OP and token.string == ";"
    return False


def compile_step(code: str, *, cell_id: str | None = None) -> dict[str, Any]:
    """What the kernel needs to run ``code`` as one cell. Never raises.

    ``{"body", "last_expr", "defs", "refs"}``. ``body`` is the code without
    its final expression statement and ``last_expr`` that expression (``None``
    when the cell does not end in one, or ends in ``;``), with marimo's
    renaming of cell-local ``_private`` names applied (prefix
    ``_cell_<cell_id>``; without ``cell_id`` the prefix is derived from the
    code). Both keep the cell's line numbers: the expression is preceded by
    blank lines and wrapped in parentheses, so tracebacks point at the cell's
    own lines. ``defs`` and ``refs`` are sorted. A cell that does not parse
    returns its code as ``body`` with ``"error": "syntax_error"``.
    """
    try:
        ast.parse(code)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return {"body": code, "last_expr": None, "defs": [], "refs": [], "error": "syntax_error"}
    if cell_id is None:
        cell_id = hashlib.sha256(code.encode("utf-8", "surrogatepass")).hexdigest()[:10]
    graph = analyze_code([(cell_id, code)])["cells"][cell_id]
    try:
        source = mangle_source(code, cell_id)
    except (MangleError, SyntaxError, ValueError, RecursionError):
        source = code
    module = ast.parse(source)
    last_expr: str | None = None
    body = source
    final = module.body[-1] if module.body else None
    if isinstance(final, ast.Expr) and not _ends_with_semicolon(source):
        lines = source.split("\n")
        start_line, start_col = final.lineno, final.col_offset
        end_line = final.end_lineno or start_line
        end_col = final.end_col_offset or 0
        prefix = lines[: start_line - 1]
        head = _byte_prefix(lines[start_line - 1], start_col)
        body = "\n".join([*prefix, head]) if head.strip() else "\n".join(prefix)
        expr_lines = lines[start_line - 1 : end_line]
        expr_lines[-1] = _byte_prefix(expr_lines[-1], end_col)
        expr_lines[0] = expr_lines[0][len(head) :]
        # Parenthesised so continuation lines may keep any indentation.
        last_expr = "\n" * (start_line - 1) + "(" + "\n".join(expr_lines) + ")"
    return {
        "body": body,
        "last_expr": last_expr,
        "defs": graph["defs"],
        "refs": graph["refs"],
    }


def _byte_prefix(line: str, byte_offset: int) -> str:
    return line.encode("utf-8")[:byte_offset].decode("utf-8", "ignore")
