# The Alkera notebook file format (`.alknb.py`), version 1.0

Normative where it says MUST, SHOULD and MAY. A reader and a writer can be implemented from this document and `NOTEBOOK_CELL_IDENTITY.md` alone, and checked against the compatibility corpus (section 11). The rationale is in Appendix A.

## 1. Summary

An Alkera notebook is a marimo notebook file (a Python module whose cells are decorated functions) with two additions:

1. a fenced settings block in the header (`# >>> alkera` ... `# <<< alkera`), and
2. a stable id on every cell, carried as the first keyword of the cell's decorator (`alkera_id="..."`).

Stock marimo opens and runs the file. The format builds on marimo's notebook format and session snapshot, nbformat 4.5's cell id pattern, TOML 1.0, and PEP 723 (which it leaves untouched and does not extend).

## 2. Files

- Extension `.alknb.py`. A file with that name whose content is not a marimo notebook is invalid; a reader reports it and does not treat it as a notebook.
- Encoding UTF-8. A byte order mark, CRLF line endings and a missing trailing newline are accepted on read; writers emit UTF-8 without a byte order mark, LF line endings and a trailing newline.

## 3. Layout

In order:

1. Optional shebang line, then optional PEP 263 coding line.
2. Header: any comments, PEP 723 blocks and a module docstring, kept verbatim, plus exactly one settings block (section 4). The settings block follows the shebang, the coding line and any PEP 723 `script` block; otherwise it is first.
3. `import marimo`
4. `__generated_with = "<marimo version>"` (informational; not a format version)
5. `app = marimo.App(<keywords>)` with marimo's app configuration keywords, only non-default ones
6. Optional setup cell: `with app.setup(alkera_id="<id>"):` and its body
7. Cells (section 5)
8. `if __name__ == "__main__":` / `    app.run()`

Statements outside these positions are violations; a writer drops them as marimo's writer does, except executable statements in the header, which a reader reports and a writer keeps.

Example:

```python
# >>> alkera
# format = "1.0"
# reactivity = "autorun"
# dataframe = "polars"
# env = "default"
# <<< alkera
import marimo

__generated_with = "0.25.1"
app = marimo.App(width="medium")

with app.setup(alkera_id="a1b2c3d4e5"):
    import alkera
    import polars as pl


@app.cell(alkera_id="m2n3p4q5r6")
def _():
    orders = alkera.sql(
        rf"""
        SELECT order_id, region, amount FROM analytics.orders
        """,
        connection="Warehouse",
    )
    return (orders,)


@app.cell(alkera_id="s7t8v9w0x1")
def _(orders):
    weekly = orders.group_by("region").agg(pl.col("amount").sum())
    weekly
    return (weekly,)


if __name__ == "__main__":
    app.run()
```

## 4. The settings block

- Opening line exactly `# >>> alkera`; closing line exactly `# <<< alkera`; every line between is `#` or `# ` followed by text. The text with the comment prefix removed is a TOML 1.0 document.
- This block is not a PEP 723 block and does not match PEP 723's regular expression. Tools that do not know it see comments.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `format` | string `"MAJOR.MINOR"` | `"1.0"` | the format version |
| `reactivity` | `"autorun"` or `"lazy"` | `"autorun"` | whether running a cell re-runs its descendants (`autorun`) or only marks them stale (`lazy`) |
| `dataframe` | `"polars"`, `"pandas"`, `"auto"` | `"auto"` | the frame type SQL cells return; writers that create a notebook SHOULD record a concrete value |
| `env` | `"default"`, `"script"`, or a relative path starting with `./` or `../` | absent | the environment the notebook runs in: the host's default, the file's own PEP 723 `script` block, or a project folder or virtual environment directory; absent means the host decides |
| `outputs_in_git` | boolean | `false` | whether saved outputs are meant for version control |
| `autoreload` | `"off"` or `"on"` | `"off"` | whether the host reloads changed local modules before each run |
| `sql_row_limit` | integer from 1 to 10,000,000 | absent | the most rows a host keeps from a SQL cell whose query has no `LIMIT`; absent means no cut |

- Unknown keys MUST be preserved and written back after the known keys, in their original order and spelling.
- Known keys are written in the order of the table; keys equal to their default are omitted, except `format`.
- A value of the wrong type is a violation; the reader uses the default and does not fail.

## 5. Cells

### 5.1 Forms

marimo's cell forms, each carrying `alkera_id`:

```
with app.setup(alkera_id="<id>"):                                 # at most one, first
@app.cell(alkera_id="<id>"[, <alkera keywords>][, <marimo config>])
def <name>(<derived parameters>):
    <body>
    return <derived tuple>
@app.function(alkera_id="<id>")
def <function>(...): ...
@app.class_definition(alkera_id="<id>")
class <Class>: ...
app._unparsable_cell(
    r"""
    <text>
    """,
    name="<name>",
    alkera_id="<id>",
)
```

- Nothing may appear between a decorator and its `def` or `class`.
- `<name>` is `_` or a Python identifier; it is marimo's cell name and is independent of the id.
- Decorator keywords, in order: `alkera_id`; other keywords whose name starts with `alkera_` (reserved for this format; values are Python literal constants, `str`, `int`, `float`, `True`, `False` or `None`, as `ast.literal_eval` reads them and `repr` writes them; readers preserve unknown ones); then marimo's cell configuration (`column`, `disabled`, `hide_code`, `expand_output`), only when non-default.
- Parameters and the `return` tuple of `@app.cell` functions are derived from the cell's references and definitions; readers ignore them and writers regenerate them as marimo's writer does.

### 5.2 Cell ids

Normative: `NOTEBOOK_CELL_IDENTITY.md`. Ids match `^[0-9a-hjkmnp-tv-z]{10}$`; writers write one on every cell; readers resolve missing or duplicate ids by that document's algorithm, never by position.

### 5.3 Kinds

A reader derives a kind for every cell; the kind is not stored.

| Kind | Form | Editor text ("source") |
| --- | --- | --- |
| `setup` | `with app.setup` | the body |
| `python` | `@app.cell` | the body |
| `function` | `@app.function` | the whole `def` |
| `class` | `@app.class_definition` | the whole `class` |
| `sql` | `@app.cell` whose body is exactly the SQL template | the SQL text |
| `markdown` | `@app.cell` whose body is exactly the Markdown template | the Markdown text |
| `unparsable` | `app._unparsable_cell` | the raw text |

A cell is `sql` or `markdown` only when rendering its source through the template reproduces its body byte for byte; otherwise it is `python`. A deviation costs the specialised editor, never code.

SQL template (`<i>` is the body's indentation plus four spaces; optional lines in brackets, in this order):

```
<var> = alkera.sql(
<i>rf"""
<i><each SQL line>
<i>""",
[<i>connection="<name>",]
[<i>output=False,]
)
```

- `<var>` is an identifier. The query is a raw f-string: backslashes are literal, `{expr}` interpolates a Python expression, literal braces are written `{{` and `}}`. A SQL text containing `"""` cannot be represented and is a `python` cell.
- `connection` names a connection the host resolves; without it the query runs on DuckDB over the frames in scope. A name the host cannot resolve, or one the reader may not use, is refused when the cell runs; it never falls back to DuckDB.
- A server resolves the name among the connections of the workspace that holds the notebook. The name is the connection's identity there: a server that lets a connection's name change makes it a different connection, and a cell that names the old one shows it as missing. The cell stores the name, not a record id (Appendix A).
- The web editor changes `connection`, `<var>` and `output=False` with the document operation `set_meta`, which validates each key against the cell's kind (`connection`, `output_var`, `show_output` for SQL; `quote` for Markdown) and re-renders the cell from the template. A key set to `null` returns to its default: no `connection`, `_df`, shown.

Markdown template: `alkera.md(` newline, `<i>r"""` (or `rf"""` when interpolating), each Markdown line indented by `<i>`, `<i>"""`, `)`. Markdown containing `"""` is a `python` cell.

### 5.4 Cells a reader cannot parse

A cell whose code the reader cannot parse (for example syntax newer than the reader's Python) is `unparsable` for analysis, and a writer MUST write it back exactly as it was read, decorator and body bytes unchanged. Files that are not valid Python are read through marimo's scanner fallback; no code is ever dropped.

### 5.5 The runtime module

SQL and Markdown cells are calls into the `alkera` module, and `alkera.ui.*` builds widgets, so every notebook may name `alkera` without importing it.

- A kernel MUST make `alkera` resolvable in the notebook's namespace before any cell runs, the way a builtin is: a cell may bind the name itself, and when that cell's names are cleared the module is visible again. The kernel imports the module on first use, so a notebook that never names it loads nothing. That import never takes the module from the notebook's own folder: no cell asked for it, so a file of the same name beside the notebook is not what a SQL or Markdown cell runs. A cell that writes `import alkera` itself gets Python's usual import rules.
- A canonical file imports it: the setup block contains the line `import alkera`. That line is what lets the file run under plain Python and stock marimo, where nothing pre-binds the name.
- A writer MUST add the import when some cell reads the name `alkera` and no cell binds it (an import, an assignment, a function or class of that name). The line goes first in the notebook's setup block; a notebook with no setup block gets one holding only that line, with an id minted from its code so that writing the same notebook twice gives the same file. A setup block kept verbatim (section 5.4) is left alone. A writer never adds the import to a notebook that does not use the module.
- Tools that create a notebook (`alkera-notebook new`, the agent's `notebook.create`, the Files pane's "New notebook") write the setup block with the import from the start. `packages/alkera-notebook/tests/vectors/new_notebook.json` holds the text of a new notebook; the command line and the web client are both tested against it.

## 6. Reading and writing

- `read(text, known)` returns the header text, settings, unknown settings, app configuration, cells (id, kind, name, source, code, config, kind metadata, other `alkera_*` keywords, how the id was resolved), and violations. `known` is an optional map from id to normalized code of a prior version (identity section 6.2).
- `write(notebook)` produces the canonical form: sections 3 to 5, with marimo's code generator for everything marimo defines.
- Guarantees: for a file a writer produced, `write(read(f)) == f`; for any readable file, `write(read(x))` is a fixed point; no reader path drops cell code. One exception to the first: a file written before section 5.5 that uses `alkera` without importing it gains the setup import the first time it is written, and is a fixed point from then on with every cell's id and code unchanged.

## 7. The graph

`analyze(notebook)` returns, per cell, `defs` and `refs` (marimo's static analysis), `sql_tables` (tables named in the query; a SQL cell's refs include a Python name only when another cell defines it), and `errors`, each an object naming what it is about (`{code: "multiple_definitions", name, cells}`, `{code: "delete_nonlocal", name, cells}`, `{code: "cycle", cells}`, `{code: "syntax_error"}`), plus edges `[a, b]` when `b` refs a name `a` defines. Canonical JSON, lists sorted. "Loads to the same graph" in section 11 means byte-identical JSON.

Python's builtins and the runtime module `alkera` (section 5.5) are provided: a cell that reads one of them has no ref for it unless another cell defines the name. A cell that binds `alkera` (the setup block's import, or any other binding) is its definer under the usual rules, so two cells binding it are `multiple_definitions`.

## 8. Saved outputs

- Location `__marimo__/session/<file name>.json` beside the notebook, in marimo's session snapshot shape (`version`, `metadata.marimo_version`, `metadata.script_metadata_hash`, `cells[{id, code_hash, outputs, console}]`). Every cell is present with its `code_hash`, and `id` is the Alkera cell id.
- Each output bundle includes `text/plain` (and `text/html` where available) beside any `application/vnd.alkera.*` type.
- An `alkera` key at the top level and on each cell holds the run (`run_id`, `trigger`, `started_at`, `finished_at`, `status`) and provenance (`code_hash`, `lineage_hash`, `env_fingerprint`): `lineage_hash = sha256(code_hash || sorted ancestor lineage_hashes)` over the code each ancestor ran with; `env_fingerprint = sha256(kind || lock or spec bytes || python version || platform tag)`. This data is informational; hosts take authoritative attribution from their own records.
- Values above 256 KiB are stored in `__marimo__/session/<file name>.d/<sha256>.<ext>` and referenced as `{"application/vnd.alkera.ref+json": {"sha256", "mime", "bytes"}}`; readers locate blobs only from `sha256` and an extension allowlist keyed by `mime`, and verify the hash.
- A snapshot without `alkera` keys (written by stock marimo) is reattached by `code_hash`, with no provenance.
- Stock marimo shows these outputs only when its version equals `metadata.marimo_version` and every cell's code hash matches (marimo's own rule).
- With `outputs_in_git = false`, the directory carries a `.gitignore` containing `*` and `!.gitignore`.

## 9. Versioning

- Minor version: additive changes an older reader can ignore (a new settings key, a new `alkera_*` keyword, a new kind whose form older readers read as `python`). A 1.x reader opens any 1.y file and preserves what it does not know.
- Major version: anything an older reader would misread. A reader opens a newer major version read-only. A writer converts older files through migrations (pure functions keyed by the source version), which are never removed.
- `__generated_with` is not a format version.

## 10. Compatibility with stock marimo

- Version floor: canonical files run on stock marimo 0.14.17 and later; earlier releases cannot call `app.setup(...)` with any keyword, so a file with a setup cell fails there. Cell keywords (`@app.cell(alkera_id=...)`, `@app.function(alkera_id=...)`) run on 0.11.3 and later. The compatibility tests pin both floors.
- Stock marimo opens and runs canonical files, which import `alkera` in their setup block (section 5.5). With the `alkera` package installed, `alkera.*` calls delegate to marimo (`alkera.md` renders, `alkera.ui.*` become `mo.ui.*`, `alkera.sql` becomes `mo.sql` with a connection from the local configuration); without it, cells that call `alkera` fail to import it.
- Stock marimo logs a warning per cell for `alkera_id`, `marimo check --strict` exits 1, and a save by stock marimo drops the keywords (readers then recover ids per identity section 6). The settings block survives verbatim.
- Stock marimo does not recognise `alkera.sql` as SQL in its static analysis, so it does not see dependencies of a local DuckDB query on frames in scope.
- marimo notebooks become Alkera notebooks by conversion (marimo's converter, then exact rewrites of `mo.md`, `mo.sql` and `mo.ui.*` to their `alkera` forms); they are not run as authored.

## 11. The compatibility corpus

- Layout: `packages/alkera-notebook/tests/format_corpus/v<MAJOR.MINOR>/<case>/notebook.alknb.py`, `expected.json` (cells, settings, violations, graph), `README.md`.
- For every case of every shipped version, today's reader returns the expected cells and graph. For cases of the current version, writing what was read reproduces the file byte for byte; every case reaches a fixed point. Expected files are never edited to make a test pass; a behaviour change that breaks an old case is fixed with a reader fix or a migration.
- Stock marimo matrix: every current case parses in stock marimo (the vendored marimo's base version and the previous two minor versions) to the same codes in the same order, and runs where it needs no external system.
- A classifier fuzz test: random SQL and Markdown round-trip through the templates; near-misses stay `python` byte for byte.

## Appendix A. Rationale (informative)

- **Why a fence and not PEP 723.** PEP 723 requires tools not to read block types it has not standardized, and creating a `script` block to hold settings would change how `uv run` treats the file. The fence carries the same TOML, survives a stock-marimo save verbatim, and does not affect marimo's `script_metadata_hash` (measured with marimo 0.25.1).
- **Why ids in decorator keywords.** See `NOTEBOOK_CELL_IDENTITY.md`, which evaluates every option with evidence.
- **Why raw f-strings for SQL.** A plain f-string turns `\n` in SQL into a newline the editor did not show and rejects a query ending in a quote; a raw f-string with the closing quotes on their own line has neither problem.
- **Why outputs beside the file.** Jupyter keeps outputs in `.ipynb`, which makes diffs noisy and commits data by accident; marimo and Deepnote keep them beside the notebook. Using marimo's snapshot shape lets stock marimo show them.
- **Why a SQL cell stores a connection's name, not its id.** The file is code a person reads, runs with stock marimo (where `connection` names an entry of the local configuration) and writes by hand; an opaque id would mean nothing in all three. Two connections reachable from one workspace under one name are ambiguous, and a server refuses that name for notebooks rather than guessing.
- **What is not in the format.** Outputs, execution counts or kernel state in the `.py`; magics; marker comments inside cell code; a second notebook syntax; per-cell languages other than Python cells of a recognised shape.
