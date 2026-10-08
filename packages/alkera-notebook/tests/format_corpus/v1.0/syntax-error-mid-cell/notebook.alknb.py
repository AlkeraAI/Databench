# >>> alkera
# format = "1.0"
# <<< alkera

import marimo

__generated_with = "0.25.1"
app = marimo.App()

@app.cell(alkera_id="00000000c1")
def _():
    a = 1
    return (a,)


@app.cell(alkera_id="00000000c2", hide_code=True)
def broken(a):
    b = (a +
    return


@app.cell(alkera_id="00000000c3")
def _(a):
    c = a * 2
    return


if __name__ == "__main__":
    app.run()
