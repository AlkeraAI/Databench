# >>> alkera
# format = "1.0"
# dataframe = "polars"
# <<< alkera

import marimo

__generated_with = "0.25.1"
app = marimo.App()


@app.cell(alkera_id="0000000081")
def _():
    x = 1
    return (x,)


@app.cell(alkera_id="0000000082")
def _():
    x = 2
    return (x,)


@app.cell(alkera_id="0000000083")
def _(x):
    print(x)
    return


if __name__ == "__main__":
    app.run()
