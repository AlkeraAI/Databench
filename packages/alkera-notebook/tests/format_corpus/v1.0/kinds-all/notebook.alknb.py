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


@app.cell(alkera_id="0000000002")
def _():
    alkera.md(
        r"""
        # Orders
        """
    )
    return


@app.cell(alkera_id="0000000003")
def _(orders):
    df = alkera.sql(
        rf"""
        SELECT * FROM orders
        """,
    )
    return (df,)


@app.cell(alkera_id="0000000004")
def _(df):
    summary = df.describe()
    summary
    return


@app.function(alkera_id="0000000005")
def double(x):
    return 2 * x


@app.class_definition(alkera_id="0000000006")
class Point:
    x: int = 0


app._unparsable_cell(
    r"""
    x = (
    """,
    name="broken", alkera_id="0000000007"
)


if __name__ == "__main__":
    app.run()
