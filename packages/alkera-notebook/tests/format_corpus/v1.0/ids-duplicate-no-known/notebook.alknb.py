# >>> alkera
# format = "1.0"
# <<< alkera

import marimo

__generated_with = "0.25.1"
app = marimo.App()

@app.cell(alkera_id="00000000e2")
def _():
    a = 1
    return


@app.cell(alkera_id="00000000e2")
def _():
    b = 2
    return


if __name__ == "__main__":
    app.run()
