"""Cell-local names, applied to source text.

marimo keeps a cell's ``_private`` names inside the cell by renaming them
(``_x`` becomes ``_cell_<id>_x``) in the syntax tree before running it. The
kernel compiles source text instead of trees, so this module applies the same
renaming to the text: it runs marimo's visitor on a parsed copy, compares it
node for node with an untouched parse to find every renamed identifier, and
rewrites exactly those spans. Line numbers do not change, so tracebacks still
point at the cell's own lines.
"""

from __future__ import annotations

import ast
import io
import tokenize
from dataclasses import dataclass

from alkera_notebook.format import _fork


class MangleError(Exception):
    """A renamed identifier whose position in the text could not be found."""


@dataclass(frozen=True)
class _Edit:
    line: int  # 1-based
    start: int  # character column
    end: int
    text: str


def _char_col(line_text: str, byte_col: int) -> int:
    return len(line_text.encode("utf-8")[:byte_col].decode("utf-8", "ignore"))


class _Tokens:
    def __init__(self, code: str) -> None:
        self.names: list[tokenize.TokenInfo] = []
        self.all: list[tokenize.TokenInfo] = list(
            tokenize.generate_tokens(io.StringIO(code).readline)
        )
        self.names = [t for t in self.all if t.type == tokenize.NAME]

    def in_span(self, start: tuple[int, int], end: tuple[int, int]) -> list[int]:
        return [i for i, t in enumerate(self.all) if start <= t.start and t.end <= end]


def _span(node: ast.AST, lines: list[str]) -> tuple[tuple[int, int], tuple[int, int]]:
    lineno = getattr(node, "lineno", 1)
    end_lineno = getattr(node, "end_lineno", None) or lineno
    col = _char_col(lines[lineno - 1], getattr(node, "col_offset", 0))
    end_col = _char_col(lines[end_lineno - 1], getattr(node, "end_col_offset", 0) or 0)
    return (lineno, col), (end_lineno, end_col)


def mangle_source(code: str, cell_id: str) -> str:
    """``code`` with its cell-local names renamed as marimo renames them.

    Raises :class:`SyntaxError` when the code does not parse and
    :class:`MangleError` when a renamed identifier cannot be located.
    """
    original = ast.parse(code)
    renamed = ast.parse(code)
    _fork.ScopedVisitor("cell_" + cell_id).visit(renamed)
    lines = code.split("\n")
    tokens = _Tokens(code)
    edits: list[_Edit] = []

    def token_edit(index: int, new: str) -> None:
        token = tokens.all[index]
        edits.append(_Edit(token.start[0], token.start[1], token.end[1], new))

    def name_tokens(node: ast.AST, old: str) -> list[int]:
        start, end = _span(node, lines)
        return [
            i
            for i in tokens.in_span(start, end)
            if tokens.all[i].type == tokenize.NAME and tokens.all[i].string == old
        ]

    for before, after in zip(ast.walk(original), ast.walk(renamed), strict=True):
        if type(before) is not type(after):
            raise MangleError("the visitor changed the tree's shape")
        if isinstance(before, ast.Name) and isinstance(after, ast.Name):
            if before.id != after.id:
                (line, col), (_, end) = _span(before, lines)
                edits.append(_Edit(line, col, end, after.id))
        elif isinstance(before, ast.arg) and isinstance(after, ast.arg):
            if before.arg != after.arg:
                (line, col), _ = _span(before, lines)
                edits.append(_Edit(line, col, col + len(before.arg), after.arg))
        elif isinstance(
            before, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ) and isinstance(after, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if before.name != after.name:
                found = [
                    i
                    for i in name_tokens(before, before.name)
                    if tokens.all[i - 1].string in ("def", "class")
                ]
                if not found:
                    raise MangleError(f"definition of {before.name!r} not found")
                token_edit(found[0], after.name)
        elif isinstance(before, ast.alias) and isinstance(after, ast.alias):
            if before.asname != after.asname and before.asname and after.asname:
                found = name_tokens(before, before.asname)
                if not found:
                    raise MangleError(f"import alias {before.asname!r} not found")
                token_edit(found[-1], after.asname)
        elif isinstance(before, ast.ExceptHandler) and isinstance(after, ast.ExceptHandler):
            if before.name != after.name and before.name and after.name:
                found = [
                    i for i in name_tokens(before, before.name) if tokens.all[i - 1].string == "as"
                ]
                if not found:
                    raise MangleError(f"exception name {before.name!r} not found")
                token_edit(found[0], after.name)
        elif isinstance(before, (ast.Global, ast.Nonlocal)) and isinstance(
            after, (ast.Global, ast.Nonlocal)
        ):
            for old, new in zip(before.names, after.names, strict=True):
                if old != new:
                    found = name_tokens(before, old)
                    if not found:
                        raise MangleError(f"declared name {old!r} not found")
                    token_edit(found[0], new)
        else:
            for field_name in ("name", "rest"):
                was = getattr(before, field_name, None)
                now = getattr(after, field_name, None)
                if isinstance(was, str) and isinstance(now, str) and was != now:
                    found = name_tokens(before, was)
                    if not found:
                        raise MangleError(f"name {was!r} not found")
                    token_edit(found[-1], now)

    out = list(lines)
    seen: set[tuple[int, int]] = set()
    for edit in sorted(edits, key=lambda e: (e.line, e.start), reverse=True):
        if (edit.line, edit.start) in seen:
            continue
        seen.add((edit.line, edit.start))
        text = out[edit.line - 1]
        out[edit.line - 1] = text[: edit.start] + edit.text + text[edit.end :]
    result = "\n".join(out)
    if ast.dump(ast.parse(result)) != ast.dump(renamed):
        raise MangleError("the rewritten text does not match marimo's renamed tree")
    return result
