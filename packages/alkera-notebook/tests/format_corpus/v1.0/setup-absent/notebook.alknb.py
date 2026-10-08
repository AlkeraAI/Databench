# >>> alkera
# format = "1.0"
# dataframe = "polars"
# <<< alkera

import marimo

__generated_with = "0.25.1"
app = marimo.App()


@app.cell(alkera_id="0000000041")
def _():
    x = 1
    return (x,)


@app.cell(alkera_id="0000000042")
def _(x):
    y = x + 1
    y
    return


if __name__ == "__main__":
    app.run()
