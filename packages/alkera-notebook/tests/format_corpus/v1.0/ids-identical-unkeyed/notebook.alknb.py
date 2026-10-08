import marimo

__generated_with = "0.25.1"
app = marimo.App()

with app.setup:
    import polars as pl


@app.cell
def _():
    x = 1
    return


@app.cell
def _():
    x = 1
    return


if __name__ == "__main__":
    app.run()
