

# >>> alkera
# format = "1.0"
# <<< alkera

import marimo

import os
__generated_with = "0.25.1"
app = marimo.App(width=os.environ.get('W', 'full'), app_title='T')


@app.cell(alkera_id="00000000f1", **{"x": 1})
def _():
    a = 1
    return


@app.cell(alkera_id="00000000f2", alkera_ref=a, alkera_list=[1], hide_code="yes")
def _():
    b = 1
    return


@app.cell(alkera_id="00000000f3", column=True, bogus=1, disabled=False)
def _():
    c = 1
    return


@app.cell(alkera_id="00000000f4")
@functools.cache
def _():
    d = 1
    return


if __name__ == "__main__":
    app.run()
