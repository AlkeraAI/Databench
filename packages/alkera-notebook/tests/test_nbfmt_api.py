"""The format API end to end on the layout the contract specifies."""

from __future__ import annotations

from alkera_notebook import format as nbformat

CANONICAL = '''\
#!/usr/bin/env -S uv run --script
# -*- coding: utf-8 -*-
# /// script
# dependencies = ["polars>=1.9"]
# ///
# >>> alkera
# format = "1.0"
# dataframe = "polars"
# env = "default"
# <<< alkera
"""Optional docstring."""

import marimo

__generated_with = "0.25.1"
app = marimo.App(width="medium")

with app.setup(alkera_id="a1b2c3d4e5"):
    import alkera
    import polars as pl


@app.cell(alkera_id="f6g7h8j9k0", hide_code=True)
def _():
    alkera.md(
        r"""
        # Weekly revenue
        """
    )
    return


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
    return


if __name__ == "__main__":
    app.run()
'''


def test_canonical_file_round_trips_byte_for_byte() -> None:
    ir = nbformat.read(CANONICAL)
    assert nbformat.write(ir) == CANONICAL


def test_cells_are_read_with_kinds_sources_and_keyword_ids() -> None:
    ir = nbformat.read(CANONICAL)
    assert [(c.id, c.kind, c.resolution) for c in ir.cells] == [
        ("a1b2c3d4e5", "setup", "keyword"),
        ("f6g7h8j9k0", "markdown", "keyword"),
        ("m2n3p4q5r6", "sql", "keyword"),
        ("s7t8v9w0x1", "python", "keyword"),
    ]
    markdown, sql = ir.cells[1], ir.cells[2]
    assert markdown.source == "# Weekly revenue"
    assert dict(markdown.config) == {"hide_code": True}
    assert sql.source == "SELECT order_id, region, amount FROM analytics.orders"
    assert sql.meta["connection"] == "Warehouse"
    assert sql.meta["output_var"] == "orders"
    assert dict(ir.settings)["dataframe"] == "polars"
    assert ir.header_text.endswith('"""Optional docstring."""\n')
    assert dict(ir.app_config) == {"width": "medium"}


def test_graph_links_definitions_to_references() -> None:
    graph = nbformat.analyze(nbformat.read(CANONICAL))
    assert ["m2n3p4q5r6", "s7t8v9w0x1"] in graph["edges"]
    assert graph["cells"]["s7t8v9w0x1"]["refs"] == ["orders", "pl"]
