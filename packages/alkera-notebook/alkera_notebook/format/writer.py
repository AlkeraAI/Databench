"""Writing a :class:`NotebookIR` as canonical ``.alknb.py`` text.

Everything marimo defines (function signatures, return tuples, the setup block,
``@app.function`` promotion, the app constructor) comes from the fork's code
generator, so a file looks the way marimo's own editor would save it, plus the
settings fence and each cell's ``alkera_id`` keyword. A cell the reader kept as
text (an ``app._unparsable_cell``, a cell with syntax the reader could not
parse, a stray statement) is written back byte for byte.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from alkera_notebook.format import _compile, _fork
from alkera_notebook.format.header import assemble_header, render_fence
from alkera_notebook.format.ids import is_cell_id, mint_id
from alkera_notebook.format.ir import CellIR, NotebookIR
from alkera_notebook.format.reader import (
    CONFIG_DEFAULTS,
    CONFIG_KEYS,
    EXTRA_PREFIX,
    ID_KEYWORD,
    is_literal,
)
from alkera_notebook.format.templates import (
    RUNTIME_IMPORT,
    RUNTIME_MODULE,
    python_string,
    setup_with_runtime,
)

_RUN_GUARD = 'if __name__ == "__main__":\n    app.run()\n'


def cell_options(cell: CellIR) -> dict[str, Any]:
    """The cell's keywords in writing order: id, other ``alkera_*``, marimo config."""
    options: dict[str, Any] = {ID_KEYWORD: cell.id}
    for key, value in cell.extra.items():
        if (
            key != ID_KEYWORD
            and key.startswith(EXTRA_PREFIX)
            and key.isidentifier()
            and is_literal(value)
        ):
            options[key] = value
    for key, value in cell.config.items():
        types = CONFIG_KEYS.get(key)
        if types is None or not isinstance(value, types):
            continue
        if key == "column" and isinstance(value, bool):
            continue
        if value != CONFIG_DEFAULTS[key]:
            options[key] = value
    return options


def _cell_name(cell: CellIR) -> str:
    name = cell.name
    if cell.kind in ("function", "class"):
        # marimo's writer names top-level cells by their definition; the
        # prefix lets it fall back to `_` when it demotes one to `@app.cell`.
        return "*" + (name if name.isidentifier() else "_")
    if name == "_" or (name.isidentifier() and name != _fork.SETUP_CELL_NAME):
        return name
    return "_"


_KEYWORD_RE = re.compile(r"""alkera_id\s*=\s*(?:"[^"\n]*"|'[^'\n]*')""")


_PROBE_HEAD = "import marimo\n\napp = marimo.App()\n\n\n@app.cell\ndef _():\n    return\n\n\n"


def _verbatim_code(verbatim: str) -> str | None:
    """The code the reader extracts from a kept cell text, read on its own.

    A cell precedes it in the probe so a kept ``with app.setup`` block is read
    in a body position, as it was in its file.
    """
    from alkera_notebook.format.reader import read

    probe = read(f"{_PROBE_HEAD}{verbatim}\n")
    if len(probe.cells) != 2:
        return None
    return probe.cells[1].code


def _with_id(verbatim: str, cell_id: str) -> str | None:
    """``verbatim`` carrying ``alkera_id="<cell_id>"``, edited in place."""
    keyword = f'alkera_id="{cell_id}"'
    if _KEYWORD_RE.search(verbatim):
        return _KEYWORD_RE.sub(keyword, verbatim, count=1)
    first, newline, rest = verbatim.partition("\n")
    for prefix in ("@app.cell", "@app.function", "@app.class_definition"):
        if first == prefix:
            return f"{first}({keyword}){newline}{rest}"
        if first.startswith(prefix + "("):
            inner = first[len(prefix) + 1 :]
            sep = "" if inner.startswith(")") else ", "
            return f"{prefix}({keyword}{sep}{inner}{newline}{rest}"
    match = re.match(r"\A((?:async\s+)?with\s+app\.setup)(\(|\s*:)", verbatim)
    if match:
        head, opener = match.group(1), match.group(2)
        tail = verbatim[match.end() :]
        if opener == "(":
            sep = "" if tail.lstrip().startswith(")") else ", "
            return f"{head}({keyword}{sep}{tail}"
        return f"{head}({keyword}):{tail}"
    if first.startswith("app._unparsable_cell(") and verbatim.endswith(")"):
        body = verbatim[:-1].rstrip()
        sep = " " if body.endswith(",") else ", "
        trailing = verbatim[len(body) : -1]
        return f"{body}{sep}{keyword}{trailing})"
    return None


def _kept_text(cell: CellIR) -> str | None:
    """The cell's kept text if it still matches the cell, with its id."""
    verbatim = cell.meta.get("verbatim")
    if not isinstance(verbatim, str) or not verbatim:
        return None
    if _verbatim_code(verbatim) != cell.code:
        return None
    if cell.meta.get("stray"):
        return verbatim
    probe = _with_id(verbatim, cell.id)
    if probe is None:
        # A stray statement cannot carry an id; it is kept without one.
        return verbatim
    if _verbatim_code(probe) != cell.code:
        return verbatim
    return probe


def file_code(code: str) -> str:
    """``code`` as a file can hold it: leading and trailing blank (or
    whitespace-only) lines removed. marimo's parser never returns them, so
    keeping them would make the written file read back as different code."""
    lines = code.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    return "\n".join(lines[start:])


def _runtime_names(code: str) -> tuple[bool, bool]:
    """``(binds, uses)``: whether ``code`` binds the runtime module's name
    and whether it reads it. Read off the syntax tree rather than the cell
    compiler, so the check leaves the compile memo the writer reuses as it
    was; code that does not parse does neither."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return False, False
    binds = uses = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == RUNTIME_MODULE:
            uses = uses or isinstance(node.ctx, ast.Load)
            binds = binds or not isinstance(node.ctx, ast.Load)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            binds = binds or any(
                (alias.asname or alias.name.split(".")[0]) == RUNTIME_MODULE for alias in node.names
            )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            binds = binds or node.name == RUNTIME_MODULE
    return binds, uses


def _uses_runtime_unimported(cells: list[CellIR]) -> bool:
    """Some cell uses the runtime module and no cell binds it."""
    uses = False
    for cell in cells:
        if cell.kind == "unparsable" or RUNTIME_MODULE not in cell.code:
            continue
        binds, used = _runtime_names(cell.code)
        if binds:
            return False
        uses = uses or used
    return uses


def with_runtime_import(cells: list[CellIR]) -> list[CellIR]:
    """``cells`` with the runtime module imported in the setup block when a
    cell uses it and none binds it: the import line goes first in the
    notebook's setup cell, or a setup cell holding only the import is added
    (its id minted from its code, so writing the same notebook twice gives
    the same file). The kernel binds the module whatever the file says; the
    import is what lets the file run under plain Python or stock marimo. A
    setup cell kept as the reader found it is left alone."""
    if not _uses_runtime_unimported(cells):
        return cells
    first = cells[0]
    if first.kind == "setup":
        if _kept_text(first) is not None:
            return cells
        code = setup_with_runtime(first.code)
        return [replace(first, code=code, source=code), *cells[1:]]
    setup = CellIR(
        id=mint_id(RUNTIME_IMPORT, {cell.id for cell in cells}),
        kind="setup",
        name=_fork.SETUP_CELL_NAME,
        source=RUNTIME_IMPORT,
        code=RUNTIME_IMPORT,
    )
    return [setup, *cells]


def _setup_split(cells: list[CellIR]) -> tuple[CellIR | None, list[CellIR]]:
    if cells and cells[0].kind == "setup" and _kept_text(cells[0]) is None:
        return cells[0], cells[1:]
    return None, cells


def write(ir: NotebookIR) -> str:
    """Canonical file text for ``ir``. Never raises.

    The writer writes each cell's ``code``; an editor that changes a SQL or
    Markdown cell's ``source`` renders the new code with :func:`render_cell`.
    """
    try:
        return _write(ir)
    except Exception:
        return _write_safe(ir)


def _app_constructor(app_config: Mapping[str, Any]) -> str:
    config = _fork.AppConfig.from_untrusted_dict(dict(app_config), silent=True)
    return str(_fork.generate_app_constructor(config))


def _preamble(ir: NotebookIR) -> list[str]:
    fence = render_fence(ir.format, ir.settings, ir.unknown_settings)
    header = assemble_header(ir.header_text, fence).rstrip("\n")
    return [
        header,
        "",
        "import marimo",
        "",
        f"__generated_with = {python_string(ir.generated_with or _fork.MARIMO_VERSION)}",
        _app_constructor(ir.app_config),
        "",
    ]


def _write(ir: NotebookIR) -> str:
    cells = with_runtime_import(list(ir.cells))
    setup, rest = _setup_split(cells)
    codes: list[str] = []
    names: list[str] = []
    configs: list[Any] = []
    if setup is not None:
        codes.append(file_code(setup.code))
        names.append(_fork.SETUP_CELL_NAME)
        configs.append(_fork.CellConfig.from_dict(cell_options(setup)))
    for cell in rest:
        codes.append(file_code(cell.code))
        names.append(_cell_name(cell))
        configs.append(_fork.CellConfig.from_dict(cell_options(cell)))

    setup_impl = _fork.pop_setup_cell(codes, names, configs)
    toplevel_defs = set(setup_impl.defs) if setup_impl is not None else set()
    # A setup cell that does not compile stays in the list as an ordinary cell.
    body_cells = rest if setup_impl is not None or setup is None else [setup, *rest]
    extraction = _fork.TopLevelExtraction(
        codes,
        names,
        configs,
        toplevel_defs,
        compiler=_compile.cell_compiler([cell.id for cell in body_cells]),
    )
    statuses = list(extraction)
    blocks: list[str] = []
    for cell, status in zip(body_cells, statuses, strict=True):
        kept = _kept_text(cell)
        blocks.append(kept if kept is not None else _fork.safe_serialize_cell(extraction, status))

    contents = [
        *_preamble(ir),
        _fork.build_setup_section(setup_impl),
        "\n\n\n".join(blocks),
        "\n",
        _RUN_GUARD,
    ]
    return "\n".join(contents)


def _write_safe(ir: NotebookIR) -> str:
    """A last-resort form: every cell as ``app._unparsable_cell`` with its code."""
    blocks = []
    for cell in ir.cells:
        cell_id = cell.id if is_cell_id(cell.id) else None
        keyword = f", alkera_id={python_string(cell_id)}" if cell_id else ""
        blocks.append(f'app._unparsable_cell({cell.code!r}, name="_"{keyword})')
    try:
        preamble = _preamble(ir)
    except Exception:
        preamble = ["import marimo", "", "app = marimo.App()", ""]
    return "\n".join([*preamble, "", "\n\n\n".join(blocks), "\n", _RUN_GUARD])
