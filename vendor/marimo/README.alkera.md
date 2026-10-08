# marimo, as vendored by Alkera

Base tag: `0.25.1` (upstream commit `92fbbbee742eccf4c88dbc2afee16167e34dc7d5`)

This directory is a squashed `git subtree` of
[marimo](https://github.com/marimo-team/marimo) at a release tag. marimo is
Apache-2.0 licensed; its `LICENSE` and `marimo/third_party*.txt` are kept as
they are.

## What is not here

`scripts/marimo-subtree.sh` filters upstream's tree before squashing it:
`docs/` (about 160 MB of images and videos), `frontend/` and `examples/` are
left out. Alkera never serves marimo's frontend (every output is rendered by
Alkera's own renderers, with no network fetch). The filtered commit is built
deterministically, so `make marimo-bump` can regenerate the base the last
squash names and merge the new release onto it.

## How it is used

`make gen-alkera-marimo` copies `vendor/marimo/marimo` to
`packages/alkera-notebook/alkera_notebook/_marimo`, a private package name, so
a person's own `marimo` install never collides with it. The copy rewrites
`marimo` import statements (and strings that name the package's own modules) to
the private name and stamps each changed file with a one-line notice. It leaves
out `marimo/_static` (the frontend bundle) and `marimo/_smoke_tests` (manual QA
notebooks); notebooks shipped as data are copied byte for byte. The copy is
committed and drift-checked.

## Maintenance

```bash
make marimo-fetch-upstream        # list releases newer than the base tag
make marimo-bump TAG=x.y.z        # squashed subtree merge; Alkera edits are kept
make marimo-show-patches          # the fenced edits below, and commits touching this tree
make gen-alkera-marimo            # then regenerate the private package
uv run pytest packages/alkera-notebook/tests -k nbfmt
```

After a bump, update the base tag at the top of this file.

## Alkera edits

Every edit is fenced in the source:

```python
# == ALKERA EDIT START <id>
...
# == ALKERA EDIT END
```

and each edited file carries a one-line notice at its top that Alkera modified
it. Each edit has a test in
`packages/alkera-notebook/tests/test_nbfmt_patches.py` that fails without it.

| Id | Files | Reason | Offered upstream |
| --- | --- | --- | --- |
| `alkera-kwargs` (M1) | `marimo/_ast/cell.py`, `marimo/_ast/codegen.py` | Cell keywords with a registered prefix (`alkera_` by default, `register_passthrough_prefix`) are kept in order in `CellConfig.passthrough` without the "Invalid config keys" warning, and the code generator writes them first in `@app.cell(...)`, `@app.function(...)`, `@app.class_definition(...)` and `with app.setup(...)`, and after `name=` in `app._unparsable_cell(...)`. This is how `.alknb.py` files carry cell ids through a save. | To offer, as a namespaced pass-through for host metadata |
| `sql-calls` (M2) | `marimo/_ast/visitor.py` | The visitor's list of calls whose argument is SQL is a module-level list with `register_sql_call`, and includes `alkera.sql`, so a query's tables become references. | To offer, as the registrable list |
| `sqlglot-refs` (M5) | `marimo/_ast/visitor.py`, `marimo/_ast/sql_visitor.py` | When DuckDB is not importable, SQL statements are split and their created and referenced tables found through sqlglot alone, so the graph does not depend on DuckDB being installed. `find_sql_refs` also treats sqlglot's `TokenError` (an unterminated quote) like a parse error instead of failing the save. | To offer |
| `ids-from-host` (M6) | `marimo/_ast/cell_manager.py` | `CellManager.cell_id_source` (default from `set_default_cell_id_source`, `None` keeps marimo's behaviour) lets a host give loaded cells its own ids, for example `passthrough_cell_id("alkera_id")`, instead of the positional generator. An id already used, or absent, falls back to the generator. | To offer |
| `indent-lines` | `marimo/_ast/codegen.py` | `indent_text` splits lines only at `\n` (as Python does). `textwrap.indent` also splits at `\x0b`, `\x0c`, `\x1c`-`\x1e`, `\x85`, `\u2028` and `\u2029`, so saving a cell with one of them inside a string literal inserted spaces into the string. Lines holding only whitespace are indented too, so whitespace inside a multi-line string survives a save. | To offer, as a bug fix |
| `nbsp-when-needed` | `marimo/_ast/compiler.py` | `compile_cell` replaces non-breaking spaces with spaces only when the code does not parse as written. It replaced them everywhere, so a non-breaking space inside a string literal (common in French text) became a space on every save. | To offer, as a bug fix |
| `trailing-comment` | `marimo/_ast/parse.py` | A comment after the last statement of a setup block, an `@app.function`, an `@app.class_definition` or a cell with no return value is part of the cell, and so is trailing whitespace on that line. The parser cut the line at the statement's end, so a save dropped them. | To offer, as a bug fix |
| `empty-setup` | `marimo/_ast/codegen.py`, `marimo/_ast/parse.py` | An empty setup cell that carries passthrough keywords (its id) is written as `with app.setup(...):` with `pass` and read back as empty, instead of vanishing from the file. Without passthrough keywords marimo's behaviour is unchanged. | To offer, with the passthrough |
| `compiler-from-host` | `marimo/_ast/toplevel.py` | `TopLevelExtraction` (and `TopLevelStatus`) take a `compiler`, `compile_cell` by default, so a host that writes the same cells on every edit can put a memo in front of the compile. The format writer passes a bounded one keyed by code, cell id and the SQL environment. | To offer, as an injectable compiler |
| `sql-defs-per-cell` | `marimo/_ast/codegen.py` | `to_functiondef` looks up only the cell's own defs to find the SQL-made ones it leaves out of the return. It walked every variable of the notebook for every cell, so writing a notebook was quadratic in its size (about a third of writing 2,000 cells). | To offer, as a performance fix |
