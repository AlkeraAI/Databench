# >>> alkera
# format = "1.0"
# dataframe = "polars"
# <<< alkera

import marimo

__generated_with = "0.25.1"
app = marimo.App()

with app.setup(alkera_id="0000000001"):
    import alkera
    import polars as pl


@app.function(alkera_id="0000000051")
def area(r: float) -> float:
    return 3.14159 * r * r


@app.class_definition(alkera_id="0000000052")
class Box:
    def __init__(self, w: int) -> None:
        self.w = w


@app.cell(alkera_id="0000000053")
def _():
    scale = 2
    return (scale,)


@app.cell(alkera_id="0000000054")
def _(scale):
    def scaled(v):
        return v * scale

    return


@app.cell(alkera_id="0000000055")
def _(scale):
    area(Box(scale).w)
    return


if __name__ == "__main__":
    app.run()
