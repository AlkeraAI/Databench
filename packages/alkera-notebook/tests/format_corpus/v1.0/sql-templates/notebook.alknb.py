# >>> alkera
# format = "1.0"
# dataframe = "polars"
# <<< alkera

import marimo

__generated_with = "0.25.1"
app = marimo.App()

with app.setup(alkera_id="0000000001"):
    import alkera
    import polars as pl


@app.cell(alkera_id="0000000011")
def _():
    df = alkera.sql(
        rf"""
        SELECT 1 AS one
        """,
    )
    return (df,)


@app.cell(alkera_id="0000000012")
def _():
    df = alkera.sql(
        rf"""
        SELECT order_id
        FROM analytics.orders
        """,
        connection="Warehouse",
    )
    return (df,)


@app.cell(alkera_id="0000000013")
def _():
    _t = alkera.sql(
        rf"""
        CREATE TABLE t AS SELECT 1
        """,
        output=False,
    )
    return


@app.cell(alkera_id="0000000014")
def _(limit):
    df = alkera.sql(
        rf"""
        SELECT * FROM df LIMIT {limit}
        """,
        connection="Lake",
        output=False,
    )
    return (df,)


@app.cell(alkera_id="0000000015")
def _():
    df = alkera.sql(
        rf"""
        SELECT '{{"a": 1}}'::JSON AS j
        """,
    )
    return (df,)


@app.cell(alkera_id="0000000016")
def _(people):
    df = alkera.sql(
        rf"""
        SELECT regexp_matches(name, '\d+\s*$') FROM people
        """,
    )
    return (df,)


@app.cell(alkera_id="0000000017")
def _(t):
    df = alkera.sql(
        rf"""
        SELECT * FROM t WHERE name = 'O''Brien'
        """,
    )
    return (df,)


@app.cell(alkera_id="0000000018")
def _(t):
    df = alkera.sql(
        rf"""
        SELECT * FROM t WHERE name = "x"
        """,
    )
    return (df,)


@app.cell(alkera_id="0000000019")
def _():
    quoted = alkera.sql('SELECT """x""" AS q')
    return


@app.cell(alkera_id="0000000020")
def _():
    empty = alkera.sql(
        rf"""

        """,
    )
    return


@app.cell(alkera_id="0000000021")
def _(t):
    df = alkera.sql(
        rf"""
        SELECT 1

          -- indented comment
        FROM t
        """,
    )
    return (df,)


if __name__ == "__main__":
    app.run()
