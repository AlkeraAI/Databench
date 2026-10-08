# >>> alkera
# format = "1.0"
# dataframe = "polars"
# <<< alkera

import marimo

__generated_with = "0.25.1"
app = marimo.App()


@app.cell(alkera_id="0000000091")
def _(c):
    a = c + 1
    return (a,)


@app.cell(alkera_id="0000000092")
def _(a):
    b = a + 1
    return (b,)


@app.cell(alkera_id="0000000093")
def _(b):
    c = b + 1
    return (c,)


@app.cell(alkera_id="0000000094")
def _():
    d = 1
    return


if __name__ == "__main__":
    app.run()
