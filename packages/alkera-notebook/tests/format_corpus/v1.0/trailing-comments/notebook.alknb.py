# >>> alkera
# format = "1.0"
# dataframe = "polars"
# <<< alkera

import marimo

__generated_with = "0.25.1"
app = marimo.App()

with app.setup(alkera_id="00000000g1"):
    import json
    import math  # note


@app.function(alkera_id="00000000g2")
def f():
    return 1  # one


@app.class_definition(alkera_id="00000000g3")
class K:
    a = 1  # attribute


@app.cell(alkera_id="00000000g4")
def _():
    y = math.pi  # pi
    return


if __name__ == "__main__":
    app.run()
