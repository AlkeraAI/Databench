"""SQL and Markdown cells: canonical templates and the exact classifier.

A cell is ``sql`` or ``markdown`` only when rendering its editor text through
the template below reproduces its code byte for byte. Anything else, however
close, is ``python`` and keeps its bytes: a deviation costs the specialised
editor, never code.

SQL (optional lines in brackets, in this order)::

    <var> = alkera.sql(
        rf\"\"\"
        <each SQL line>
        \"\"\",
    [    connection="<name>",]
    [    output=False,]
    )

Markdown::

    alkera.md(
        r\"\"\"          (or rf\"\"\" when interpolating)
        <each Markdown line>
        \"\"\"
    )

Each text line is indented by four spaces; an empty line stays empty. The
text is the body of a raw string, so backslashes are literal; with ``rf``,
``{expr}`` interpolates and literal braces are written ``{{`` and ``}}``. Text
containing three double quotes cannot be represented.
"""

from __future__ import annotations

import ast
import keyword
import re
from collections.abc import Mapping
from typing import Any

from alkera_notebook.format.ir import KINDS

INDENT = "    "
TRIPLE = '"""'
#: The module the kernel binds in every notebook's namespace before any cell
#: runs, and that the setup block imports so the file also runs under plain
#: Python or stock marimo. SQL and Markdown cells call into it.
RUNTIME_MODULE = "alkera"
#: The setup block's line that imports it.
RUNTIME_IMPORT = f"import {RUNTIME_MODULE}"
SQL_CALL = f"{RUNTIME_MODULE}.sql"
MD_CALL = f"{RUNTIME_MODULE}.md"
MARKDOWN_QUOTES = ("r", "rf")

_SQL_RE = re.compile(
    r"\A(?P<var>[^\W\d]\w*) = " + re.escape(SQL_CALL) + r"\(\n"
    r"    rf\"\"\"\n"
    r"(?P<body>(?:[^\n]*\n)*?)"
    r"    \"\"\",\n"
    r"(?P<options>(?:    [^\n]*\n)*)"
    r"\)\Z"
)
_MD_RE = re.compile(
    r"\A" + re.escape(MD_CALL) + r"\(\n"
    r"    (?P<quote>rf?)\"\"\"\n"
    r"(?P<body>(?:[^\n]*\n)*?)"
    r"    \"\"\"\n"
    r"\)\Z"
)


def _imports_runtime(code: str) -> bool:
    try:
        module = ast.parse(code)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return False
    return any(
        isinstance(node, ast.Import)
        and any(a.name == RUNTIME_MODULE and a.asname is None for a in node.names)
        for node in module.body
    )


def setup_with_runtime(code: str) -> str:
    """A setup block's code that imports the runtime module: ``code`` as it
    is when it already does, else with :data:`RUNTIME_IMPORT` as its first
    line."""
    if _imports_runtime(code):
        return code
    return f"{RUNTIME_IMPORT}\n{code}" if code.strip() else RUNTIME_IMPORT


def python_string(value: str) -> str:
    """A Python literal for ``value``, double-quoted whenever ``repr`` allows it."""
    text = repr(value)
    if text.startswith("'") and '"' not in value:
        return '"' + text[1:-1] + '"'
    return text


def python_literal(value: Any) -> str:
    """A literal constant as writers spell it: ``repr``, strings double-quoted."""
    if isinstance(value, str):
        return python_string(value)
    return repr(value)


def _indent_lines(text: str) -> str:
    return "".join((INDENT + line if line else "") + "\n" for line in text.split("\n"))


def _dedent_body(body: str) -> str | None:
    lines = body.split("\n")[:-1]
    out: list[str] = []
    for line in lines:
        if line == "":
            out.append("")
        elif line.startswith(INDENT):
            out.append(line[len(INDENT) :])
        else:
            return None
    return "\n".join(out)


def _is_identifier(name: str) -> bool:
    return name.isidentifier() and not keyword.iskeyword(name)


def sql_meta(
    output_var: str = "_df",
    connection: str | None = None,
    show_output: bool = True,
) -> dict[str, Any]:
    return {
        "output_var": output_var,
        "connection": connection,
        "engine": None,
        "show_output": show_output,
    }


def render_sql(source: str, meta: Mapping[str, Any]) -> str | None:
    var = meta.get("output_var", "_df")
    connection = meta.get("connection")
    show_output = meta.get("show_output", True)
    if TRIPLE in source or not isinstance(var, str) or not _is_identifier(var):
        return None
    if meta.get("engine") is not None:
        return None
    if connection is not None and not isinstance(connection, str):
        return None
    parts = [f"{var} = {SQL_CALL}(\n", f'{INDENT}rf"""\n', _indent_lines(source), f'{INDENT}""",\n']
    if connection is not None:
        parts.append(f"{INDENT}connection={python_string(connection)},\n")
    if show_output is False:
        parts.append(f"{INDENT}output=False,\n")
    parts.append(")")
    return "".join(parts)


def render_markdown(source: str, meta: Mapping[str, Any]) -> str | None:
    quote = meta.get("quote", "r")
    if TRIPLE in source or quote not in MARKDOWN_QUOTES:
        return None
    return f'{MD_CALL}(\n{INDENT}{quote}"""\n{_indent_lines(source)}{INDENT}"""\n)'


def render_cell(kind: str, source: str, meta: Mapping[str, Any]) -> str:
    """The cell code for editor text ``source`` of ``kind``.

    For ``sql`` and ``markdown`` the template is applied; when the text cannot
    be represented (it contains three double quotes, or the metadata is
    invalid) the text is returned as Python code unchanged, which classifies as
    ``python``. Every other kind's code is its text.
    """
    if kind == "sql":
        rendered = render_sql(source, meta)
        return source if rendered is None else rendered
    if kind == "markdown":
        rendered = render_markdown(source, meta)
        return source if rendered is None else rendered
    if kind not in KINDS:
        return source
    return source


def _classify_sql(code: str) -> tuple[str, dict[str, Any]] | None:
    match = _SQL_RE.match(code)
    if match is None:
        return None
    source = _dedent_body(match["body"])
    if source is None:
        return None
    connection: str | None = None
    show_output = True
    for line in match["options"].split("\n")[:-1]:
        name, sep, rest = line[len(INDENT) :].partition("=")
        if not sep or not rest.endswith(","):
            return None
        if name == "connection" and connection is None:
            try:
                value = ast.literal_eval(rest[:-1])
            except (ValueError, SyntaxError, MemoryError, RecursionError):
                return None
            if not isinstance(value, str):
                return None
            connection = value
        elif name == "output" and rest == "False,":
            show_output = False
        else:
            return None
    meta = sql_meta(match["var"], connection, show_output)
    if render_sql(source, meta) != code:
        return None
    return source, meta


def _classify_markdown(code: str) -> tuple[str, dict[str, Any]] | None:
    match = _MD_RE.match(code)
    if match is None:
        return None
    source = _dedent_body(match["body"])
    if source is None:
        return None
    meta: dict[str, Any] = {"quote": match["quote"]}
    if render_markdown(source, meta) != code:
        return None
    return source, meta


def classify(code: str) -> tuple[str, str, dict[str, Any]]:
    """``(kind, source, meta)`` for an ``@app.cell`` body.

    ``sql`` or ``markdown`` when the code is exactly a template rendering,
    ``python`` (with the code as its source and empty metadata) otherwise.
    Never raises.
    """
    sql = _classify_sql(code)
    if sql is not None:
        return "sql", sql[0], sql[1]
    markdown = _classify_markdown(code)
    if markdown is not None:
        return "markdown", markdown[0], markdown[1]
    return "python", code, {}
