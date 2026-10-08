# >>> alkera
# format = "1.0"
# <<< alkera

import marimo

__generated_with = "0.25.1"
app = marimo.App()

@app.cell(alkera_id="00000000c6")
def _():
    x = 1
    return


print('stray')


with app.setup:
    import json


@app.cell(alkera_id="00000000c7")
def _():
    y = 2
    return


if __name__ == "__main__":
    app.run()
