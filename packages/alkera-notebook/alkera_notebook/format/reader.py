"""Reading ``.alknb.py`` text into a :class:`NotebookIR`.

The reader walks the file with the marimo fork's own parser (so a cell's code
is exactly what marimo sees), reads each cell's keywords from its syntax tree,
keeps the text of every cell it cannot represent canonically, derives kinds,
and resolves ids. It never raises and never drops a cell's code: problems
become violations.
"""

from __future__ import annotations

import ast
import io
import math
import re
import tokenize
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from alkera_notebook.format import _fork
from alkera_notebook.format.header import parse_settings, split_header
from alkera_notebook.format.ids import CellKey, normalize_code, resolve
from alkera_notebook.format.ir import (
    FORMAT_VERSION,
    CellIR,
    Kind,
    NotebookIR,
    ReadOnlyReason,
    Violation,
)
from alkera_notebook.format.templates import classify

ID_KEYWORD = "alkera_id"
EXTRA_PREFIX = "alkera_"
CONFIG_KEYS: Mapping[str, tuple[type, ...]] = {
    "column": (int, type(None)),
    "disabled": (bool,),
    "hide_code": (bool,),
    "expand_output": (bool,),
}
CONFIG_DEFAULTS: Mapping[str, Any] = {
    "column": None,
    "disabled": False,
    "hide_code": False,
    "expand_output": False,
}


@dataclass
class Keywords:
    """A cell's keywords, split the way the format reads them."""

    cell_id: object = None
    extra: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    name: str | None = None
    problems: list[str] = field(default_factory=list)


def is_literal(value: object) -> bool:
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return True
    return isinstance(value, float) and math.isfinite(value)


def split_keywords(keywords: list[ast.keyword]) -> Keywords:
    found = Keywords()
    for keyword in keywords:
        if keyword.arg is None:
            found.problems.append("`**` keywords are not allowed on a cell")
            continue
        try:
            value = ast.literal_eval(keyword.value)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            found.problems.append(f"`{keyword.arg}` is not a literal constant")
            continue
        if keyword.arg == ID_KEYWORD:
            found.cell_id = value
        elif keyword.arg.startswith(EXTRA_PREFIX):
            if is_literal(value):
                found.extra[keyword.arg] = value
            else:
                found.problems.append(f"`{keyword.arg}` must be a str, int, float, bool or None")
        elif keyword.arg in CONFIG_KEYS:
            if isinstance(value, CONFIG_KEYS[keyword.arg]) and not (
                keyword.arg == "column" and isinstance(value, bool)
            ):
                if value != CONFIG_DEFAULTS[keyword.arg]:
                    found.config[keyword.arg] = value
            else:
                found.problems.append(f"`{keyword.arg}` has a value of the wrong type")
        elif keyword.arg == "name":
            if isinstance(value, str):
                found.name = value
        else:
            found.problems.append(f"unknown cell keyword `{keyword.arg}` (dropped)")
    return found


def node_keywords(node: ast.AST) -> list[ast.keyword]:
    """The keywords of a cell node's decorator, ``with`` call or call."""
    target: ast.AST | None = None
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        target = _fork.marimo_parse.get_valid_decorator(node)
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        target = node.items[0].context_expr
    elif isinstance(node, ast.Expr):
        target = node.value
    if isinstance(target, ast.Call):
        return list(target.keywords)
    return []


_HEAD_DEF_RE = re.compile(r"^(?:async\s+)?(?:def|class)\s")
_HEAD_WITH_RE = re.compile(r"\A(?:async\s+)?with\s+(app\.setup(?:\(.*?\))?)\s*:", re.S)


def head_keywords(block: str) -> list[ast.keyword]:
    """Keywords from the head of a cell's text when its body does not parse."""
    try:
        if block.startswith("@"):
            lines = block.split("\n")
            head: list[str] = []
            for line in lines:
                if _HEAD_DEF_RE.match(line):
                    break
                head.append(line)
            expr = ast.parse("\n".join(head)[1:].strip(), mode="eval").body
        else:
            match = _HEAD_WITH_RE.match(block)
            if match is None:
                return []
            expr = ast.parse(match.group(1), mode="eval").body
    except SyntaxError:
        return []
    return list(expr.keywords) if isinstance(expr, ast.Call) else []


def find_marimo_import(text: str) -> int | None:
    """0-based line of the module-level ``import marimo`` statement."""
    try:
        previous: tokenize.TokenInfo | None = None
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if (
                previous is not None
                and previous.type == tokenize.NAME
                and previous.string == "import"
                and previous.start[1] == 0
                and token.type == tokenize.NAME
                and token.string == "marimo"
            ):
                return previous.start[0] - 1
            if token.type not in (tokenize.COMMENT, tokenize.NL):
                previous = token
    except (tokenize.TokenError, SyntaxError, IndentationError):
        pass
    match = re.search(r"^import marimo\b", text, re.M)
    return None if match is None else text.count("\n", 0, match.start())


def _header_statements(region: str, first_line: int) -> list[Violation]:
    try:
        tree = ast.parse(region)
    except SyntaxError as error:
        return [
            Violation(
                "header_statement",
                first_line + (error.lineno or 1) - 1,
                "the text before `import marimo` is not valid Python (kept as written)",
            )
        ]
    return [
        Violation(
            "header_statement",
            first_line + node.lineno - 1,
            "an executable statement before `import marimo` (kept as written)",
        )
        for node in tree.body
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
    ]


@dataclass
class RawCell:
    """A cell as the walk found it, before kinds and ids."""

    form: str  # "setup", "cell", "function", "class_definition", "unparsable", "stray"
    code: str
    name: str
    line: int
    keywords: Keywords
    verbatim: str | None = None
    syntax_error: bool = False


class _Walk:
    """The cell walk, mirroring marimo's ``parse_notebook`` node for node."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.parser = _fork.marimo_parse.Parser(text)
        contents = self.parser.extractor.contents
        stripped_prefix = text[: len(text) - len(text.lstrip())]
        self.lead = stripped_prefix.count("\n")
        self.lines = self.parser.extractor.lines
        self.cells: list[RawCell] = []
        self.violations: list[Violation] = []
        self.app_options: dict[str, Any] = {}
        self.generated_with = ""
        self.valid = bool(contents)
        self._scanned: dict[int, tuple[int, int]] | None = None

    def file_line(self, line: int) -> int:
        return line + self.lead

    def _scanned_spans(self) -> dict[int, tuple[int, int]]:
        if self._scanned is None:
            scan = _fork.scan_notebook(self.parser.extractor.contents)
            self._scanned = {c.start_line: (c.start_line, c.end_line) for c in scan.cells}
        return self._scanned

    def _block(self, start: int, end: int) -> str:
        lines = self.lines[start - 1 : end]
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join(lines)

    def _node_span(self, node: ast.AST) -> tuple[int, int]:
        start = getattr(node, "lineno", 1)
        decorators = getattr(node, "decorator_list", None) or []
        if decorators:
            start = min(start, *(d.lineno for d in decorators))
        end = getattr(node, "end_lineno", None) or start
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.With)):
            # Trailing comments in the body belong to the cell.
            while end < len(self.lines) and (
                not self.lines[end].strip() or self.lines[end][:1] in (" ", "\t")
            ):
                end += 1
        return start, end

    def _add(self, node: ast.stmt | ast.expr, form: str, code: str, name: str) -> None:
        start, end = self._node_span(node)
        scanned = node.lineno in self.parser._scanner_generated_lines
        verbatim: str | None = None
        if scanned:
            span = self._scanned_spans().get(node.lineno, (start, end))
            verbatim = self._block(*span)
            keywords = split_keywords(head_keywords(verbatim))
            form = "unparsable"
        else:
            keywords = split_keywords(node_keywords(node))
            if form in ("unparsable", "stray"):
                verbatim = self._block(start, end)
        if form == "unparsable" and keywords.name is not None:
            name = keywords.name
        self.cells.append(
            RawCell(
                form=form,
                code=code,
                name=name,
                line=self.file_line(start),
                keywords=keywords,
                verbatim=verbatim,
                syntax_error=scanned,
            )
        )
        for problem in keywords.problems:
            self.violations.append(Violation("cell_keyword", self.file_line(start), problem))

    def _stray(self, node: ast.stmt | ast.expr) -> None:
        start, end = self._node_span(node)
        block = self._block(start, end)
        if _fork.marimo_parse.is_setup_cell(node):
            self.violations.append(
                Violation(
                    "setup_not_first",
                    self.file_line(start),
                    "a setup cell after other cells is kept as written and not run as setup",
                )
            )
        else:
            self.violations.append(
                Violation(
                    "body_statement",
                    self.file_line(start),
                    "a statement between cells is kept as written; marimo ignores it",
                )
            )
        self.cells.append(
            RawCell("stray", block, "_", self.file_line(start), Keywords(), verbatim=block)
        )

    def run(self) -> None:
        mp = _fork.marimo_parse
        parser = self.parser
        if not self.valid:
            return
        try:
            body = parser.node_stack()
        except SyntaxError:
            # marimo's scanner fallback parses the text before the first cell
            # on its own and raises when that text is not Python.
            self.valid = False
            return
        parser.parse_header(body)
        imported = parser.parse_import(body)
        if not imported:
            self.valid = False
            return
        alias = "marimo"
        import_node = imported.unwrap()
        if isinstance(import_node, ast.Import) and import_node.names[0].asname:
            alias = import_node.names[0].asname
            self.violations.append(
                Violation(
                    "marimo_import_alias",
                    self.file_line(import_node.lineno),
                    "`marimo` is imported under another name",
                )
            )
        version = parser.parse_version(body)
        if version:
            self.generated_with = str(version.unwrap())
        else:
            self.violations.append(
                Violation("generated_with", 0, "`__generated_with` is missing or malformed")
            )
        app = parser.parse_app(body, import_alias=alias)
        if not app:
            self.valid = False
            return
        for violation in app.violations:
            if violation.description == mp.UNEXPECTED_KEYWORD_VALUE_VIOLATION:
                self.violations.append(
                    Violation(
                        "app_keyword",
                        self.file_line(violation.lineno),
                        "a `marimo.App` keyword whose value is not a constant (dropped)",
                    )
                )
            else:
                self.violations.append(
                    Violation(
                        "body_statement",
                        self.file_line(violation.lineno),
                        "a statement between `import marimo` and `marimo.App` (dropped)",
                    )
                )
        self.app_options = dict(app.unwrap().options)

        # Statements before the first cell; the first cell may be the setup.
        while True:
            upcoming = body.peek()
            if upcoming is None or mp.is_cell(upcoming):
                break
            node = next(body)
            if node is None:
                break
            if mp.is_run_guard(node):
                return
            self._stray(node)
        upcoming = body.peek()
        if upcoming is not None and mp.is_setup_cell(upcoming):
            next(body)
            setup = parser.extractor.to_setup_cell(upcoming).unwrap()
            self._add(upcoming, "setup", setup.code, _fork.SETUP_CELL_NAME)
        guarded = False
        while (node := next(body)) is not None:
            if mp.is_body_cell(node):
                result = parser.extractor.to_cell(node)
                for violation in result.violations:
                    if violation.description.startswith("Multiple decorators"):
                        self.violations.append(
                            Violation(
                                "decorator_between",
                                self.file_line(violation.lineno),
                                "only the cell decorator may precede a cell's definition",
                            )
                        )
                cell = result.unwrap()
                form = "unparsable"
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    decorator = mp.get_valid_decorator(node)
                    attr = None
                    if isinstance(decorator, ast.Call):
                        attr = getattr(decorator.func, "attr", None)
                    elif isinstance(decorator, ast.Attribute):
                        attr = decorator.attr
                    form = attr or "cell"
                name = cell.name
                if form == "unparsable":
                    # The scanner and `app._unparsable_cell(name=...)` carry it here.
                    name = str(getattr(cell, "options", {}).get("name") or name)
                if form in ("function", "class_definition"):
                    name = getattr(node, "name", name)
                self._add(node, form, cell.code, name)
            elif mp.is_run_guard(node):
                guarded = True
                break
            else:
                self._stray(node)
        if not guarded:
            self.violations.append(
                Violation("run_guard", 0, 'the `if __name__ == "__main__"` guard is missing')
            )


_FORM_KIND: Mapping[str, Kind] = {
    "setup": "setup",
    "function": "function",
    "class_definition": "class",
    "unparsable": "unparsable",
    "stray": "unparsable",
}


def _cell_name(raw: RawCell) -> str:
    name = raw.name
    if raw.form == "setup":
        return _fork.SETUP_CELL_NAME
    if name == "_" or (name.isidentifier() and not name.startswith("*")):
        return name
    return "_"


def _known_major(fmt: str) -> int:
    return int(fmt.split(".", 1)[0])


def read(text: str, *, known: Mapping[str, str] | None = None) -> NotebookIR:
    """Read notebook text. Never raises; problems are returned as violations.

    ``known`` maps cell id to normalized code for the last version of this
    notebook the caller trusts (the live document, git ``HEAD``); it lets ids
    survive a save by a tool that dropped the ``alkera_id`` keywords.
    """
    try:
        return _read(text, known)
    except Exception as error:
        return NotebookIR(
            header_text=text if isinstance(text, str) else "",
            violations=(
                Violation("internal_error", 0, f"the reader failed: {type(error).__name__}"),
            ),
            read_only_reason="unreadable",
        )


def _read(text: str, known: Mapping[str, str] | None) -> NotebookIR:
    violations: list[Violation] = []
    if text.startswith("\ufeff"):
        text = text[1:]
        violations.append(Violation("byte_order_mark", 1, "a byte order mark was removed"))
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        violations.append(Violation("line_endings", 0, "line endings were normalised to LF"))
    # marimo strips the file before parsing, so indentation on the first
    # non-blank line means nothing to it. The writer puts the settings block
    # above the header, where that indentation would be a syntax error, so it
    # is dropped here (the leading line breaks stay, keeping line numbers).
    body = text.lstrip()
    text = "\n" * text.count("\n", 0, len(text) - len(body)) + body

    lines = text.split("\n")
    import_line = find_marimo_import(text)
    region = "\n".join(lines[: import_line if import_line is not None else len(lines)])
    parts = split_header(region)
    violations.extend(parts.violations)
    settings = parse_settings(parts.fence_body, parts.fence_line)
    if parts.fence_body is None:
        violations.append(Violation("settings_missing", 0, "no `# >>> alkera` settings block"))
    violations.extend(settings.violations)
    read_only: ReadOnlyReason | None = None
    if _known_major(settings.format) > _known_major(FORMAT_VERSION):
        read_only = "newer_format"
        violations.append(
            Violation(
                "newer_format",
                parts.fence_line,
                f"format {settings.format} is newer than this reader's {FORMAT_VERSION}; read only",
            )
        )

    if import_line is None:
        violations.append(
            Violation("not_a_notebook", 0, "no `import marimo` and `marimo.App` definition")
        )
        return NotebookIR(
            format=settings.format,
            header_text=parts.header_text,
            settings=settings.values,
            unknown_settings=settings.unknown,
            violations=violations,
            # Nothing was read, so nothing may be written over the text.
            read_only_reason=read_only or "not_a_notebook",
        )
    violations.extend(_header_statements(region, 1))

    walk = _Walk(text)
    walk.run()
    if not walk.valid:
        violations.append(
            Violation("not_a_notebook", 0, "no `marimo.App` definition after `import marimo`")
        )
        read_only = read_only or "not_a_notebook"
    violations.extend(walk.violations)

    keys: list[CellKey] = []
    for raw in walk.cells:
        keyword = raw.keywords.cell_id
        keys.append(
            CellKey(
                keyword=keyword if isinstance(keyword, str) else None,
                code=normalize_code(raw.code),
            )
        )
        if raw.form != "stray" and keyword is None:
            violations.append(Violation("id_missing", raw.line, "the cell has no `alkera_id`"))
    report = resolve(keys, known)
    for index, raw in enumerate(walk.cells):
        if raw.keywords.cell_id is not None and (
            index in report.malformed or not isinstance(raw.keywords.cell_id, str)
        ):
            violations.append(
                Violation(
                    "id_malformed",
                    raw.line,
                    f"`alkera_id={raw.keywords.cell_id!r}` is not a cell id",
                )
            )
    for index in report.duplicates:
        violations.append(
            Violation(
                "id_duplicate",
                walk.cells[index].line,
                "this cell's `alkera_id` is used by another cell; it gets a new id",
            )
        )

    cells: list[CellIR] = []
    for raw, resolved in zip(walk.cells, report.cells, strict=True):
        meta: dict[str, Any] = {}
        if raw.form == "cell":
            kind_name, source, meta = classify(raw.code)
            kind: Kind = "python"
            if kind_name == "sql":
                kind = "sql"
            elif kind_name == "markdown":
                kind = "markdown"
        else:
            kind = _FORM_KIND[raw.form]
            source = raw.code
        if raw.syntax_error:
            violations.append(
                Violation(
                    "syntax_error",
                    raw.line,
                    "this reader cannot parse the cell; it is kept exactly as written",
                )
            )
        if raw.verbatim is not None:
            meta = {"verbatim": raw.verbatim}
            if raw.form == "stray":
                # Not a cell to marimo: a statement between cells, or a setup
                # block after them. Kept in place; it cannot carry an id.
                meta["stray"] = True
        cells.append(
            CellIR(
                id=resolved.id,
                kind=kind,
                name=_cell_name(raw),
                source=source,
                code=raw.code,
                config=raw.keywords.config,
                meta=meta,
                extra=raw.keywords.extra,
                resolution=resolved.resolution,
            )
        )

    violations.sort(key=lambda v: v.line)
    return NotebookIR(
        format=settings.format,
        header_text=parts.header_text,
        settings=settings.values,
        set_settings=settings.set_keys,
        unknown_settings=settings.unknown,
        app_config=walk.app_options,
        generated_with=walk.generated_with,
        cells=cells,
        violations=violations,
        read_only_reason=read_only,
    )


__all__ = ["find_marimo_import", "read", "split_keywords"]
