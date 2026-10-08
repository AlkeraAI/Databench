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


@app.cell(alkera_id="00000000a1")
def _():
    alkera.md(
        r"""
        # Café ☕ 🧪

        שלום עולם — مرحبا

        é vs é
        """
    )
    return


@app.cell(alkera_id="00000000a2")
def _():
    größe = '𝔘𝔫𝔦𝔠𝔬𝔡𝔢'
    emoji = '👩🏽‍🔬'
    return


@app.cell(alkera_id="00000000a3")
def _():
    df = alkera.sql(
        rf"""
        SELECT 'ünïcödé 🦆' AS label
        """,
    )
    return


if __name__ == "__main__":
    app.run()
