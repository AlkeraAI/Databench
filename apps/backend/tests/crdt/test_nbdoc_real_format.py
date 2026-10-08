"""The notebook document type against the real format API
(``alkera_notebook.format``): ``.alknb.py`` files in, the same files out, and
the outside changes a session meets (a stock marimo save, a VS Code save
while someone types, a ``git pull``)."""

from __future__ import annotations

import pytest
from alkera_notebook import format as fmt
from backend.services.crdt.sandbox import core
from backend.services.crdt.sandbox import notebook as nb
from backend.services.crdt.sandbox import notebook_ops as nbo
from backend.services.crdt.sandbox.core import DocCache, SandboxError
from loro import ExportMode, LoroDoc

pytestmark = [pytest.mark.spread]

KEY = "org:notebook:real"
SEED, TAB, MERGE = 4000, 5000, 4100

HEAD = '''# >>> alkera
# format = "1.0"
# dataframe = "polars"
# <<< alkera
"""Weekly revenue."""

import marimo

__generated_with = "0.25.1"
app = marimo.App(width="medium")

'''
TAIL = """

if __name__ == "__main__":
    app.run()
"""

SETUP = """with app.setup(alkera_id="a1b2c3d4e5"):
    import alkera
    import polars as pl
"""
TITLE = '''@app.cell(alkera_id="f6g7h8j9k0", hide_code=True)
def _():
    alkera.md(
        r"""
        # Weekly revenue
        """
    )
    return
'''
ORDERS = '''@app.cell(alkera_id="m2n3p4q5r6")
def _():
    orders = alkera.sql(
        rf"""
        SELECT order_id, region, amount FROM analytics.orders
        """,
        connection="Warehouse",
    )
    return (orders,)
'''
WEEKLY = """@app.cell(alkera_id="s7t8v9w0x1")
def _(orders):
    weekly = orders.group_by("region").agg(pl.col("amount").sum())
    weekly
    return
"""


def notebook(*cells: str) -> str:
    return fmt.write(fmt.read(HEAD + "\n\n".join(cells) + TAIL))


FULL = notebook(SETUP, TITLE, ORDERS, WEEKLY)


def _seed(text: str) -> tuple[DocCache, core.Seeded]:
    cache = DocCache()
    return cache, core.seed(cache, key=KEY, epoch=1, rules=nb.NOTEBOOK, text=text, peer=SEED)


def _rendered(cache: DocCache, log_seq: int) -> str:
    data = core.content(cache, key=KEY, epoch=1, log_seq=log_seq, rules=nb.NOTEBOOK)
    assert data is not None
    return data.decode()


def _tab(snapshot: bytes) -> LoroDoc:
    doc = core.new_doc()
    doc.peer_id = TAB
    doc.import_(snapshot)
    return doc


def _type(cache: DocCache, tab: LoroDoc, cell_id: str, at: int, text: str, log_seq: int) -> None:
    before = tab.oplog_vv
    source = nb.child(nb.child(tab.get_map("cells"), cell_id), "source")
    source.insert(at, text)
    tab.commit()
    verdict = core.validate(
        cache,
        key=KEY,
        epoch=1,
        log_seq=log_seq,
        rules=nb.NOTEBOOK,
        peers=frozenset({TAB}),
        update=bytes(tab.export(ExportMode.Updates(before))),
    )
    assert verdict is not None and verdict.outcome == "ok", verdict
    assert core.advance(cache, key=KEY, epoch=1, log_seq=log_seq + 1, delta=verdict.delta)


def _merge(cache: DocCache, log_seq: int, base: bytes, text: str) -> None:
    merged = core.merge(
        cache,
        key=KEY,
        epoch=1,
        log_seq=log_seq,
        rules=nb.NOTEBOOK,
        peer=MERGE + log_seq,
        base_vv=base,
        text=text,
    )
    assert merged is not None and merged.outcome == "ok", merged
    assert core.advance(cache, key=KEY, epoch=1, log_seq=log_seq + 1, delta=merged.delta)


def _sources(text: str) -> list[tuple[str, str, str]]:
    return [(c.id, c.kind, c.source) for c in fmt.read(text).cells]


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(FULL, id="setup-markdown-sql-python"),
        pytest.param(notebook(WEEKLY), id="one-cell"),
        pytest.param(
            '# /// script\n# dependencies = ["polars"]\n# ///\n' + FULL,
            id="pep723-block",
        ),
        pytest.param(
            FULL.replace('# dataframe = "polars"\n', '# dataframe = "polars"\n# theme = "dark"\n'),
            id="unknown-setting",
        ),
        pytest.param(
            FULL.replace(
                '@app.cell(alkera_id="s7t8v9w0x1")',
                '@app.cell(alkera_id="s7t8v9w0x1", alkera_pinned=True)',
            ),
            id="extra-keyword",
        ),
        pytest.param(
            notebook(
                SETUP,
                '@app.cell(alkera_id="m2n3p4q5r6")\ndef _():\n    x = (\n    return\n',
            ),
            id="unparsable-cell",
        ),
    ],
)
def test_a_seeded_file_renders_back_as_the_format_writes_it(text: str) -> None:
    canonical = fmt.write(fmt.read(text))
    cache, _ = _seed(text)
    assert _rendered(cache, 0) == canonical


def test_a_stock_marimo_save_without_ids_keeps_every_id() -> None:
    cache, seeded = _seed(FULL)
    saved = FULL.replace('alkera_id="f6g7h8j9k0", ', "")
    for cell_id in ("m2n3p4q5r6", "s7t8v9w0x1"):
        saved = saved.replace(f'(alkera_id="{cell_id}")', "")
    saved = saved.replace("SELECT order_id", "SELECT DISTINCT order_id")
    assert saved.count("alkera_id") == 1
    _merge(cache, 0, seeded.base_vv, saved)
    assert [c[0] for c in _sources(_rendered(cache, 1))] == [
        "a1b2c3d4e5",
        "f6g7h8j9k0",
        "m2n3p4q5r6",
        "s7t8v9w0x1",
    ]
    assert "SELECT DISTINCT order_id" in _rendered(cache, 1)


def test_a_vs_code_edit_concurrent_with_live_typing_keeps_both() -> None:
    cache, seeded = _seed(FULL)
    tab = _tab(seeded.snapshot)
    _type(cache, tab, "s7t8v9w0x1", 0, "# by region\n", 0)
    vscode = FULL.replace('pl.col("amount").sum()', 'pl.col("amount").sum().alias("total")')
    _merge(cache, 1, seeded.base_vv, vscode)
    weekly = dict((c[0], c[2]) for c in _sources(_rendered(cache, 2)))["s7t8v9w0x1"]
    assert weekly.startswith("# by region\n")
    assert '.alias("total")' in weekly


def test_a_git_pull_that_reorders_cells_moves_them_and_keeps_typing() -> None:
    cache, seeded = _seed(FULL)
    tab = _tab(seeded.snapshot)
    _type(cache, tab, "f6g7h8j9k0", len("# Weekly revenue"), "!", 0)
    pulled = notebook(SETUP, ORDERS, WEEKLY, TITLE)
    _merge(cache, 1, seeded.base_vv, pulled)
    after = _sources(_rendered(cache, 2))
    assert [c[0] for c in after] == ["a1b2c3d4e5", "m2n3p4q5r6", "s7t8v9w0x1", "f6g7h8j9k0"]
    assert after[3][2] == "# Weekly revenue!"


def test_typing_sql_in_an_sql_cell_renders_through_the_template() -> None:
    cache, seeded = _seed(FULL)
    tab = _tab(seeded.snapshot)
    _type(cache, tab, "m2n3p4q5r6", len("SELECT "), "DISTINCT ", 0)
    rendered = _rendered(cache, 1)
    orders = next(c for c in fmt.read(rendered).cells if c.id == "m2n3p4q5r6")
    assert orders.kind == "sql" and orders.source.startswith("SELECT DISTINCT order_id")
    assert 'connection="Warehouse"' in rendered


def test_a_newer_major_format_cannot_be_opened_or_merged_in() -> None:
    newer = FULL.replace('# format = "1.0"', '# format = "2.0"')
    with pytest.raises(SandboxError) as refused:
        _seed(newer)
    assert refused.value.code == "not_editable"
    cache, seeded = _seed(FULL)
    merged = core.merge(
        cache,
        key=KEY,
        epoch=1,
        log_seq=0,
        rules=nb.NOTEBOOK,
        peer=MERGE,
        base_vv=seeded.base_vv,
        text=newer,
    )
    assert merged is not None and (merged.outcome, merged.reason) == ("reject", "newer_format")


def test_an_inserted_sql_cell_is_written_with_the_sql_template() -> None:
    cache, _ = _seed(FULL)
    applied = nbo.apply_ops(
        cache,
        key=KEY,
        epoch=1,
        log_seq=0,
        peer=MERGE,
        base_vv=None,
        ops=[
            {
                "op": "insert",
                "kind": "sql",
                "source": "SELECT 1 AS one",
                "meta": {"output_var": "one", "connection": None},
                "after": "s7t8v9w0x1",
            }
        ],
    )
    assert applied is not None and applied.outcome == "ok"
    assert core.advance(cache, key=KEY, epoch=1, log_seq=1, delta=applied.delta)
    rendered = _rendered(cache, 1)
    (created,) = applied.result["created"]
    cell = next(c for c in fmt.read(rendered).cells if c.id == created)
    assert (cell.kind, cell.source) == ("sql", "SELECT 1 AS one")
    assert "one = alkera.sql(" in rendered


#: Files no notebook can be read from (an agent's slip with a file tool, an
#: editor's half-save). Each used to read as a notebook with no cells.
NOT_NOTEBOOKS = [
    pytest.param("", id="empty"),
    pytest.param("import os\nprint(os.getcwd())\n", id="plain_script"),
    pytest.param(FULL.replace('marimo.App(width="medium")', "marimo.App("), id="app_unclosed"),
    pytest.param("  x = 1\n y = 2\n" + FULL, id="indent_error_top"),
]


@pytest.mark.parametrize("text", NOT_NOTEBOOKS)
def test_a_file_with_no_notebook_is_never_seeded_as_an_empty_one(text: str) -> None:
    with pytest.raises(SandboxError) as refused:
        _seed(text)
    assert refused.value.code == "not_editable"


@pytest.mark.parametrize("text", NOT_NOTEBOOKS)
def test_a_non_notebook_arriving_mid_session_deletes_no_cell(text: str) -> None:
    cache, seeded = _seed(FULL)
    merged = core.merge(
        cache,
        key=KEY,
        epoch=1,
        log_seq=0,
        rules=nb.NOTEBOOK,
        peer=MERGE,
        base_vv=seeded.base_vv,
        text=text,
    )
    assert merged is not None and (merged.outcome, merged.reason) == ("reject", "not_a_notebook")
    assert _sources(_rendered(cache, 0)) == _sources(FULL)
