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


app._unparsable_cell(
    r"""
    def f(:
        pass
    """,
    name="half_written", alkera_id="0000000061"
)


app._unparsable_cell(
    r"""
    x = [1, 2
    """,
    column=1, disabled=False, hide_code=False, expand_output=False, name="_", alkera_id="0000000062"
)


if __name__ == "__main__":
    app.run()
