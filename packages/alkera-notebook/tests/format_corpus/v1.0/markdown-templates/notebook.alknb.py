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


@app.cell(alkera_id="0000000031")
def _():
    alkera.md(
        r"""
        # Title

        Some *text*.
        """
    )
    return


@app.cell(alkera_id="0000000032")
def _(total):
    alkera.md(
        rf"""
        Total: {total:,}
        """
    )
    return


@app.cell(alkera_id="0000000033")
def _():
    alkera.md(
        rf"""
        A set: {{1, 2}}
        """
    )
    return


@app.cell(alkera_id="0000000034")
def _():
    alkera.md(
        r"""
        Escapes stay: \n \t $\alpha$
        """
    )
    return


@app.cell(alkera_id="0000000035")
def _():
    alkera.md(
        r"""

        """
    )
    return


@app.cell(alkera_id="0000000036")
def _():
    alkera.md(
        r"""
          indented first line
        	tab line
        """
    )
    return


@app.cell(alkera_id="0000000037")
def _():
    alkera.md("""say \"\"\"hi\"\"\"""")
    return


if __name__ == "__main__":
    app.run()
