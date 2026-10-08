# >>> alkera
# format = "1.0"
# dataframe = "polars"
# <<< alkera

import marimo

__generated_with = "0.25.1"
app = marimo.App()

with app.setup(alkera_id="00000000g9"):
    import os


@app.cell(alkera_id="00000000g7")
def _():
    x = 1
    return


@app.cell(alkera_id="00000000g8")
def _():
    y = 2   
    return


if __name__ == "__main__":
    app.run()
