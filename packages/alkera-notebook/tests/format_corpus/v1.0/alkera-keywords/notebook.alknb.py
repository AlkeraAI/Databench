# >>> alkera
# format = "1.0"
# dataframe = "polars"
# <<< alkera

import marimo

__generated_with = "0.25.1"
app = marimo.App()


@app.cell(
    alkera_id="0000000071",
    alkera_pinned=True,
    alkera_owner=None,
    alkera_weight=1.5,
    hide_code=True,
)
def _():
    x = 1
    return


@app.cell(
    alkera_id="0000000072",
    alkera_rank=3,
    alkera_note='it\'s "quoted"',
    column=2,
    disabled=True,
    expand_output=True,
)
def _():
    y = 2
    return


if __name__ == "__main__":
    app.run()
