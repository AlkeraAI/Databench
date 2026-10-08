"""Build the notebook format compatibility corpus.

Every case lives in ``v<MAJOR.MINOR>/<case>/``: ``notebook.alknb.py``, a
``README.md`` saying what the case pins, ``expected.json`` (what the reader
returns and the graph), and for identity cases ``known.json`` (the known prior
state passed to ``read``).

Cases come in two kinds. *Writer-produced* cases are built here as notebooks
and written with today's writer; reading them must give the file back byte for
byte. *Hand-written* cases are literal text with deliberate deviations; reading
them must reach a fixed point.

This script only adds what is missing. An existing file that differs from what
it would write is reported and the script exits 1, because the corpus is a
record: a reader or writer change that alters an old case is fixed in code (or
with a migration), not by regenerating the case. ``--bless CASE`` rewrites one
case on purpose, for a reviewed change.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alkera_notebook.format import CellIR, NotebookIR, analyze, read, render_cell, write
from alkera_notebook.format.ids import mint_id

ROOT = Path(__file__).resolve().parent
VERSION = "v1.0"


@dataclass(frozen=True)
class Case:
    readme: str
    text: str
    canonical: bool
    known: Mapping[str, str] | None = None
    max_python: str | None = None
    tags: tuple[str, ...] = field(default=())


def cell(
    kind: str,
    source: str,
    *,
    cid: str | None = None,
    name: str = "_",
    meta: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> CellIR:
    meta = dict(meta or {})
    code = render_cell(kind, source, meta)
    if kind == "sql":
        meta = {"output_var": "_df", "connection": None, "engine": None, "show_output": True} | meta
    return CellIR(
        id=cid or mint_id(kind + "\0" + source, set()),
        kind=kind,  # type: ignore[arg-type]
        name=name,
        source=source,
        code=code,
        config=config or {},
        meta=meta,
        extra=extra or {},
    )


def notebook(
    *cells: CellIR,
    header: str = "",
    settings: Mapping[str, Any] | None = None,
    unknown: str = "",
    app: Mapping[str, Any] | None = None,
    fmt: str = "1.0",
) -> NotebookIR:
    return NotebookIR(
        format=fmt,
        header_text=header,
        settings={"dataframe": "polars"} if settings is None else settings,
        unknown_settings=unknown,
        app_config=app or {},
        generated_with="0.25.1",
        cells=cells,
    )


def written(readme: str, ir: NotebookIR, **extra: Any) -> Case:
    return Case(readme=readme, text=write(ir), canonical=True, **extra)


def hand(readme: str, text: str, **extra: Any) -> Case:
    return Case(readme=readme, text=text, canonical=False, **extra)


SETUP = cell("setup", "import alkera\nimport polars as pl", cid="0000000001")
FENCE = '# >>> alkera\n# format = "1.0"\n# <<< alkera\n'
HEAD = 'import marimo\n\n__generated_with = "0.25.1"\napp = marimo.App()\n\n'
GUARD = '\n\nif __name__ == "__main__":\n    app.run()\n'


def sql(source: str, cid: str, **meta: Any) -> CellIR:
    return cell("sql", source, cid=cid, meta={"output_var": "df", **meta})


def md(source: str, cid: str, quote: str = "r") -> CellIR:
    return cell("markdown", source, cid=cid, meta={"quote": quote})


STOCK_SAVED = (
    HEAD
    + "with app.setup:\n    import polars as pl\n\n\n"
    + "@app.cell\ndef _():\n    df = pl.read_csv('orders.csv')\n    return (df,)\n\n\n"
    + "@app.cell\ndef _(df):\n    total = df['amount'].sum()\n    return (total,)\n\n\n"
    + "@app.cell\ndef _(total):\n    print(f'total: {total}')\n    return\n"
    + GUARD
)
KNOWN_STOCK = {
    "aaaaaaaaa1": "import polars as pl",
    "aaaaaaaaa2": "df = pl.read_csv('orders.csv')",
    "aaaaaaaaa3": "total = df['amount'].sum()",
    "aaaaaaaaa4": "print(f'total: {total}')",
}


def stock(*bodies: str, setup: str = "import polars as pl") -> str:
    blocks = [f"@app.cell\ndef _():\n    {body.replace(chr(10), chr(10) + '    ')}\n    return\n" for body in bodies]
    return HEAD + f"with app.setup:\n    {setup}\n\n\n" + "\n\n".join(blocks) + GUARD


CASES: dict[str, Case] = {
    "kinds-all": written(
        "Every kind in one notebook: setup, python, function, class, sql, markdown and an "
        "unparsable cell, each with its keyword id.",
        notebook(
            SETUP,
            md("# Orders", "0000000002"),
            sql("SELECT * FROM orders", "0000000003"),
            cell("python", "summary = df.describe()\nsummary", cid="0000000004"),
            cell("function", "def double(x):\n    return 2 * x", cid="0000000005", name="double"),
            cell("class", "class Point:\n    x: int = 0", cid="0000000006", name="Point"),
            cell(
                "unparsable",
                "x = (",
                cid="0000000007",
                name="broken",
                meta={},
            ),
        ),
    ),
    "sql-templates": written(
        "The SQL template in every form: with and without a connection, output=False, rf "
        "interpolation, doubled braces, backslashes, a query ending in a quote, and a query "
        'containing three double quotes, which stays a python cell.',
        notebook(
            SETUP,
            sql("SELECT 1 AS one", "0000000011"),
            sql("SELECT order_id\nFROM analytics.orders", "0000000012", connection="Warehouse"),
            sql("CREATE TABLE t AS SELECT 1", "0000000013", output_var="_t", show_output=False),
            sql("SELECT * FROM df LIMIT {limit}", "0000000014", connection="Lake", show_output=False),
            sql("SELECT '{{\"a\": 1}}'::JSON AS j", "0000000015"),
            sql("SELECT regexp_matches(name, '\\d+\\s*$') FROM people", "0000000016"),
            sql("SELECT * FROM t WHERE name = 'O''Brien'", "0000000017"),
            sql('SELECT * FROM t WHERE name = "x"', "0000000018"),
            cell(
                "python",
                "quoted = alkera.sql('SELECT \"\"\"x\"\"\" AS q')",
                cid="0000000019",
            ),
            sql("", "0000000020", output_var="empty"),
            sql("SELECT 1\n\n  -- indented comment\nFROM t", "0000000021"),
        ),
    ),
    "markdown-templates": written(
        "The Markdown template: r and rf quotes, interpolation, doubled braces, backslashes, "
        "blank lines, empty text, and Markdown containing three double quotes (python).",
        notebook(
            SETUP,
            md("# Title\n\nSome *text*.", "0000000031"),
            md("Total: {total:,}", "0000000032", quote="rf"),
            md("A set: {{1, 2}}", "0000000033", quote="rf"),
            md("Escapes stay: \\n \\t $\\alpha$", "0000000034"),
            md("", "0000000035"),
            md("  indented first line\n\ttab line", "0000000036"),
            cell("python", 'alkera.md("""say \\"\\"\\"hi\\"\\"\\"""")', cid="0000000037"),
        ),
    ),
    "setup-absent": written(
        "A notebook without a setup cell.",
        notebook(cell("python", "x = 1", cid="0000000041"), cell("python", "y = x + 1\ny", cid="0000000042")),
    ),
    "functions-and-classes": written(
        "@app.function and @app.class_definition cells, a cell that uses them, and a function "
        "that refers to another cell's name (written as an @app.cell).",
        notebook(
            SETUP,
            cell("function", "def area(r: float) -> float:\n    return 3.14159 * r * r", cid="0000000051", name="area"),
            cell("class", "class Box:\n    def __init__(self, w: int) -> None:\n        self.w = w", cid="0000000052", name="Box"),
            cell("python", "scale = 2", cid="0000000053"),
            cell("python", "def scaled(v):\n    return v * scale", cid="0000000054"),
            cell("python", "area(Box(scale).w)", cid="0000000055"),
        ),
    ),
    "unparsable-named": written(
        "Unparsable cells with a name and marimo configuration, written back byte for byte.",
        notebook(
            SETUP,
            cell("unparsable", "def f(:\n    pass", cid="0000000061", name="half_written"),
            cell("unparsable", "x = [1, 2", cid="0000000062", config={"column": 1}),
        ),
    ),
    "prelude-full": written(
        "A shebang, a coding line, a PEP 723 block, a licence comment and a docstring, with the "
        "settings fence placed after the PEP 723 block.",
        notebook(
            SETUP,
            header=(
                "#!/usr/bin/env -S uv run --script\n"
                "# -*- coding: utf-8 -*-\n"
                "# /// script\n"
                '# requires-python = ">=3.11"\n'
                '# dependencies = ["polars>=1.9"]\n'
                "# ///\n"
                "# Copyright 2026 Example Co.\n"
                "# SPDX-License-Identifier: Apache-2.0\n"
                '"""Weekly revenue, by region."""\n'
            ),
        ),
    ),
    "settings-every-key": written(
        "Every known settings key with a non-default value, plus unknown keys (a scalar, an "
        "array and a table) kept after them in their original order.",
        notebook(
            SETUP,
            settings={
                "reactivity": "lazy",
                "dataframe": "pandas",
                "env": "../envs/analytics",
                "outputs_in_git": True,
                "autoreload": "on",
            },
            unknown='future_key = "kept"\nlist_key = [1, 2, 3]\n\n[future.table]\nnested = true',
        ),
    ),
    "alkera-keywords": written(
        "Unknown alkera_* keywords with True, None, 1.5, an int and a string with both quote "
        "kinds, kept in order after alkera_id and before marimo's configuration.",
        notebook(
            cell(
                "python",
                "x = 1",
                cid="0000000071",
                extra={"alkera_pinned": True, "alkera_owner": None, "alkera_weight": 1.5},
                config={"hide_code": True},
            ),
            cell(
                "python",
                "y = 2",
                cid="0000000072",
                extra={"alkera_rank": 3, "alkera_note": "it's \"quoted\""},
                config={"disabled": True, "column": 2, "expand_output": True},
            ),
        ),
    ),
    "app-options": written(
        "Every marimo.App option set to a non-default value.",
        notebook(
            SETUP,
            app={
                "width": "full",
                "app_title": "Revenue",
                "layout_file": "layouts/revenue.grid.json",
                "css_file": "custom.css",
                "html_head_file": "head.html",
                "auto_download": ["html", "ipynb"],
                "sql_output": "polars",
            },
        ),
    ),
    "multiple-definitions": written(
        "Two cells define the same name: both carry multiple_definitions.",
        notebook(cell("python", "x = 1", cid="0000000081"), cell("python", "x = 2", cid="0000000082"), cell("python", "print(x)", cid="0000000083")),
    ),
    "cycle": written(
        "Three cells that depend on each other in a cycle; a fourth outside it.",
        notebook(
            cell("python", "a = c + 1", cid="0000000091"),
            cell("python", "b = a + 1", cid="0000000092"),
            cell("python", "c = b + 1", cid="0000000093"),
            cell("python", "d = 1", cid="0000000094"),
        ),
    ),
    "unicode": written(
        "Non-BMP characters, combining marks and right-to-left text in code, SQL and Markdown.",
        notebook(
            SETUP,
            md("# Café ☕ 🧪\n\nשלום עולם — مرحبا\n\ne\u0301 vs é", "00000000a1"),
            cell("python", "größe = '𝔘𝔫𝔦𝔠𝔬𝔡𝔢'\nemoji = '👩🏽\u200d🔬'", cid="00000000a2"),
            sql("SELECT 'ünïcödé 🦆' AS label", "00000000a3"),
        ),
    ),
    "string-whitespace-and-separators": written(
        "Whitespace-only lines and characters str.splitlines treats as line breaks (form feed, "
        "group separator, U+2028) inside string literals, in Python, SQL and Markdown cells.",
        notebook(
            cell("python", 'doc = """\nfirst\n   \n\tlast\n"""\nsep = "a\u2028b\x0cc\x1dd"', cid="00000000a4"),
            sql("SELECT 1\n   \nFROM t  ", "00000000a5"),
            md("line one  \n \nline\u2028two", "00000000a6"),
        ),
    ),
    "trailing-comments": written(
        "A comment after the last statement of a setup block, an @app.function, an "
        "@app.class_definition and a cell without a return value stays with its cell.",
        notebook(
            cell("setup", "import json\nimport math  # note", cid="00000000g1", name="setup"),
            cell("function", "def f():\n    return 1  # one", cid="00000000g2", name="f"),
            cell("class", "class K:\n    a = 1  # attribute", cid="00000000g3", name="K"),
            cell("python", "y = math.pi  # pi", cid="00000000g4"),
        ),
    ),
    "empty-setup": written(
        "An empty setup cell keeps its place and id (written as `pass`).",
        notebook(cell("setup", "", cid="00000000g5", name="setup"), cell("python", "x = 1", cid="00000000g6")),
    ),
    "trailing-blank-lines": written(
        "Cells whose editor text ends in blank or whitespace-only lines are written without "
        "them, and trailing spaces on a line with code are kept.",
        notebook(
            cell("setup", "import os\n\n", cid="00000000g9", name="setup"),
            cell("python", "x = 1\n\n", cid="00000000g7"),
            cell("python", "y = 2   \n  \n\t", cid="00000000g8"),
        ),
    ),
    # ---- hand-written ------------------------------------------------------------
    "fence-misplaced": hand(
        "The settings fence after the docstring and a comment; writers move it to the top.",
        '"""Doc."""\n# a comment\n# >>> alkera\n# format = "1.0"\n# reactivity = "lazy"\n# <<< alkera\n\n'
        + HEAD
        + '@app.cell(alkera_id="00000000b1")\ndef _():\n    x = 1\n    return\n'
        + GUARD,
    ),
    "settings-invalid": hand(
        "Known settings with wrong types (defaults apply, each a violation), an invalid format "
        "string, and a second fence kept as comments.",
        "# >>> alkera\n# format = 1\n# reactivity = 3\n# dataframe = \"arrow\"\n# env = \"/abs/path\"\n"
        "# outputs_in_git = \"yes\"\n# autoreload = true\n# keep = 1\n# <<< alkera\n"
        "# >>> alkera\n# format = \"1.0\"\n# <<< alkera\n\n" + HEAD
        + '@app.cell(alkera_id="00000000b2")\ndef _():\n    return\n' + GUARD,
    ),
    "settings-not-toml": hand(
        "A fence whose body is not valid TOML: a known key on its own line still counts, the "
        "rest is kept as written.",
        '# >>> alkera\n# format = "1.0"\n# reactivity = "lazy"\n# this is = = not toml\n# <<< alkera\n\n'
        + HEAD + '@app.cell(alkera_id="00000000b3")\ndef _():\n    return\n' + GUARD,
    ),
    "settings-missing": hand(
        "A marimo notebook with keyword ids but no settings fence: defaults apply and the "
        "writer adds the fence.",
        HEAD + '@app.cell(alkera_id="00000000b4")\ndef _():\n    x = 1\n    return\n' + GUARD,
    ),
    "syntax-error-mid-cell": hand(
        "A cell with a syntax error in the middle of the file is read through marimo's scanner, "
        "kept byte for byte, and the cells around it parse normally.",
        FENCE + "\n" + HEAD
        + '@app.cell(alkera_id="00000000c1")\ndef _():\n    a = 1\n    return (a,)\n\n\n'
        + '@app.cell(alkera_id="00000000c2", hide_code=True)\ndef broken(a):\n    b = (a +\n    return\n\n\n'
        + '@app.cell(alkera_id="00000000c3")\ndef _(a):\n    c = a * 2\n    return\n'
        + GUARD,
    ),
    "python-314-syntax": hand(
        "A cell with syntax only Python 3.14 accepts (except without parentheses). On older "
        "interpreters it is unparsable and written back byte for byte.",
        FENCE + "\n" + HEAD
        + '@app.cell(alkera_id="00000000c4")\ndef _():\n    try:\n        pass\n'
        + "    except ValueError, TypeError:\n        pass\n    return\n"
        + GUARD,
        max_python="3.13",
    ),
    "header-statement": hand(
        "An executable statement and comments before import marimo: kept as written, reported.",
        "# notes\nimport os\nos.environ.setdefault('X', '1')\n\n" + FENCE + "\n" + HEAD
        + '@app.cell(alkera_id="00000000c5")\ndef _():\n    return\n' + GUARD,
    ),
    "stray-statements": hand(
        "Statements between cells (and a setup block after cells) are kept as written in their "
        "place, never dropped.",
        FENCE + "\n" + HEAD
        + '@app.cell(alkera_id="00000000c6")\ndef _():\n    x = 1\n    return\n\n\n'
        + "print('stray')\n\n\n"
        + 'with app.setup:\n    import json\n\n\n'
        + '@app.cell(alkera_id="00000000c7")\ndef _():\n    y = 2\n    return\n'
        + GUARD,
    ),
    "crlf": hand(
        "CRLF line endings: read as LF (a violation), written with LF.",
        (FENCE + "\n" + HEAD + '@app.cell(alkera_id="00000000d1")\ndef _():\n    x = 1\n    return\n' + GUARD).replace("\n", "\r\n"),
    ),
    "bom": hand(
        "A byte order mark: removed on read (a violation).",
        "\ufeff" + FENCE + "\n" + HEAD + '@app.cell(alkera_id="00000000d2")\ndef _():\n    x = 1\n    return\n' + GUARD,
    ),
    "tabs": hand(
        "Cell bodies indented with tabs; codes are read dedented and written with spaces.",
        FENCE + "\n" + HEAD
        + '@app.cell(alkera_id="00000000d3")\ndef _():\n\tx = 1\n\tif x:\n\t\ty = 2\n\treturn\n'
        + GUARD,
    ),
    "no-trailing-newline": hand(
        "No newline at the end of the file.",
        (FENCE + "\n" + HEAD + '@app.cell(alkera_id="00000000d4")\ndef _():\n    x = 1\n    return\n' + GUARD).rstrip("\n"),
    ),
    "newer-major": hand(
        "A file declaring format 2.0: read only, cells still read.",
        '# >>> alkera\n# format = "2.0"\n# future_only = true\n# <<< alkera\n\n' + HEAD
        + '@app.cell(alkera_id="00000000d5")\ndef _():\n    x = 1\n    return\n' + GUARD,
    ),
    "newer-minor": hand(
        "A file declaring format 1.7 with keys and keywords this reader does not know: read and "
        "written normally, everything kept.",
        '# >>> alkera\n# format = "1.7"\n# new_setting = "x"\n# <<< alkera\n\n' + HEAD
        + '@app.cell(alkera_id="00000000d6", alkera_future=1)\ndef _():\n    x = 1\n    return\n' + GUARD,
    ),
    "not-a-notebook": hand(
        "A plain Python script: no cells, its text kept as the header.",
        "import os\n\nprint(os.getcwd())\n",
    ),
    "marimo-alias-and-cell-forms": hand(
        "Stock marimo forms: `import marimo as mo`, bare decorators, async cells, a named cell, "
        "and a setup block with hide_code.",
        "import marimo as mo\n\n__generated_with = \"0.20.0\"\napp = mo.App(width=\"medium\")\n\n"
        "with app.setup(hide_code=True):\n    import asyncio\n\n\n"
        "@app.cell\nasync def _():\n    await asyncio.sleep(0)\n    return\n\n\n"
        "@app.cell\ndef load_data():\n    data = [1, 2, 3]\n    return (data,)\n\n\n"
        "@app.function\ndef helper():\n    return 1\n" + GUARD,
    ),
    "keywords-and-statements-invalid": hand(
        "Cell keywords the format refuses (a ** mapping, a non-literal value, a list, a wrong "
        "type for marimo's configuration, an unknown keyword), an App keyword that is not a "
        "constant, a statement between import marimo and the App, a decorator between the "
        "cell decorator and its def, and blank lines before everything.",
        "\n\n" + FENCE + "\nimport marimo\n\nimport os\n__generated_with = \"0.25.1\"\n"
        "app = marimo.App(width=os.environ.get('W', 'full'), app_title='T')\n\n\n"
        '@app.cell(alkera_id="00000000f1", **{"x": 1})\ndef _():\n    a = 1\n    return\n\n\n'
        '@app.cell(alkera_id="00000000f2", alkera_ref=a, alkera_list=[1], hide_code="yes")\n'
        "def _():\n    b = 1\n    return\n\n\n"
        '@app.cell(alkera_id="00000000f3", column=True, bogus=1, disabled=False)\n'
        "def _():\n    c = 1\n    return\n\n\n"
        '@app.cell(alkera_id="00000000f4")\n@functools.cache\ndef _():\n    d = 1\n    return\n'
        + GUARD,
    ),
    "setup-syntax-error": hand(
        "A setup block the reader cannot parse: kept byte for byte with its keywords.",
        FENCE + "\n" + HEAD
        + 'with app.setup(alkera_id="00000000f5", hide_code=True):\n    import (\n\n\n'
        + '@app.cell(alkera_id="00000000f6")\ndef _():\n    x = 1\n    return\n'
        + GUARD,
    ),
    "only-run-guard": hand(
        "A notebook with no cells at all.",
        FENCE + "\n" + HEAD + GUARD.lstrip("\n"),
    ),
    # ---- identity ----------------------------------------------------------------
    "ids-duplicate-with-known": hand(
        "A cell copied by text (duplicate keyword) with a known state: the copy whose code equals "
        "the known code keeps the id, the other gets a new one.",
        FENCE + "\n" + HEAD
        + '@app.cell(alkera_id="00000000e1")\ndef _():\n    x = 1  # edited copy\n    return\n\n\n'
        + '@app.cell(alkera_id="00000000e1")\ndef _():\n    x = 1\n    return\n' + GUARD,
        known={"00000000e1": "x = 1"},
    ),
    "ids-duplicate-no-known": hand(
        "Duplicate keywords without a known state: the earlier cell keeps the id.",
        FENCE + "\n" + HEAD
        + '@app.cell(alkera_id="00000000e2")\ndef _():\n    a = 1\n    return\n\n\n'
        + '@app.cell(alkera_id="00000000e2")\ndef _():\n    b = 2\n    return\n' + GUARD,
    ),
    "ids-malformed": hand(
        "Malformed keywords (wrong length, excluded letters, upper case, not a string) are "
        "ignored with a violation; those cells get minted ids.",
        FENCE + "\n" + HEAD
        + '@app.cell(alkera_id="short")\ndef _():\n    a = 1\n    return\n\n\n'
        + '@app.cell(alkera_id="iiiiiiiiii")\ndef _():\n    b = 1\n    return\n\n\n'
        + '@app.cell(alkera_id="ABCDEFGHJK")\ndef _():\n    c = 1\n    return\n\n\n'
        + '@app.cell(alkera_id=12345)\ndef _():\n    d = 1\n    return\n' + GUARD,
    ),
    "ids-identical-unkeyed": hand(
        "Two identical cells without keywords and no known state: two distinct minted ids, "
        "stable across runs.",
        stock("x = 1", "x = 1"),
    ),
    "ids-stripped-unchanged": hand(
        "Keywords stripped by a stock save, no structural change: every cell matches exactly.",
        STOCK_SAVED,
        known=KNOWN_STOCK,
    ),
    "ids-stripped-reordered": hand(
        "Keywords stripped and cells reordered: every cell matches exactly, ids follow the code.",
        HEAD + "with app.setup:\n    import polars as pl\n\n\n"
        + "@app.cell\ndef _(total):\n    print(f'total: {total}')\n    return\n\n\n"
        + "@app.cell\ndef _():\n    df = pl.read_csv('orders.csv')\n    return (df,)\n\n\n"
        + "@app.cell\ndef _(df):\n    total = df['amount'].sum()\n    return (total,)\n" + GUARD,
        known=KNOWN_STOCK,
    ),
    "ids-stripped-insert-delete": hand(
        "Keywords stripped, one cell deleted and an unrelated one inserted at the same place: "
        "the new cell is minted, never given the deleted cell's id.",
        HEAD + "with app.setup:\n    import polars as pl\n\n\n"
        + "@app.cell\ndef _():\n    url = 'https://example.com/data.json'\n    return (url,)\n\n\n"
        + "@app.cell\ndef _(df):\n    total = df['amount'].sum()\n    return (total,)\n\n\n"
        + "@app.cell\ndef _(total):\n    print(f'total: {total}')\n    return\n" + GUARD,
        known=KNOWN_STOCK,
    ),
    "ids-stripped-small-edit": hand(
        "Keywords stripped and about 30% of one cell edited: it keeps its id by similarity.",
        STOCK_SAVED.replace("total = df['amount'].sum()", "total = df['amount'].mean()"),
        known=KNOWN_STOCK,
    ),
    "ids-stripped-rewrite": hand(
        "Keywords stripped and one cell rewritten beyond recognition: it gets a new id.",
        STOCK_SAVED.replace("total = df['amount'].sum()", "import numpy as np\n    total = np.zeros(3)"),
        known=KNOWN_STOCK,
    ),
    "ids-stripped-no-known": hand(
        "Keywords stripped and no known state: every cell is minted deterministically.",
        STOCK_SAVED,
    ),
}


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def expected_for(case: Case) -> dict[str, Any]:
    ir = read(case.text, known=case.known)
    return {
        "canonical": case.canonical,
        "max_python": case.max_python,
        "notebook": {
            "format": ir.format,
            "header_text": ir.header_text,
            "settings": _plain(ir.settings),
            "unknown_settings": ir.unknown_settings,
            "app_config": _plain(ir.app_config),
            "generated_with": ir.generated_with,
            "read_only_reason": ir.read_only_reason,
            "cells": [
                {
                    "id": c.id,
                    "kind": c.kind,
                    "name": c.name,
                    "source": c.source,
                    "code": c.code,
                    "config": _plain(c.config),
                    "meta": _plain(c.meta),
                    "extra": _plain(c.extra),
                    "resolution": c.resolution,
                }
                for c in ir.cells
            ],
            "violations": [[v.code, v.line] for v in ir.violations],
        },
        "graph": analyze(ir),
    }


def _dump(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def case_files(name: str, case: Case) -> dict[str, bytes]:
    files = {
        "notebook.alknb.py": case.text.encode("utf-8"),
        "README.md": f"# {name}\n\n{case.readme}\n".encode(),
        "expected.json": _dump(expected_for(case)).encode("utf-8"),
    }
    if case.known is not None:
        files["known.json"] = _dump(dict(case.known)).encode("utf-8")
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the notebook format corpus.")
    parser.add_argument("--bless", action="append", default=[], help="rewrite this case")
    args = parser.parse_args()
    drift: list[str] = []
    for name, case in CASES.items():
        if case.max_python is not None and sys.version_info[:2] > tuple(
            int(p) for p in case.max_python.split(".")
        ):
            continue
        directory = ROOT / VERSION / name
        directory.mkdir(parents=True, exist_ok=True)
        for filename, content in case_files(name, case).items():
            path = directory / filename
            if path.exists() and path.read_bytes() != content and name not in args.bless:
                drift.append(f"{VERSION}/{name}/{filename}")
                continue
            if not path.exists() or name in args.bless:
                path.write_bytes(content)
    if drift:
        print("corpus files differ from what today's code produces:", file=sys.stderr)
        for item in drift:
            print(f"  {item}", file=sys.stderr)
        print("fix the reader or writer; bless a case only for a reviewed change", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
