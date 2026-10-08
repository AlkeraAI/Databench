# >>> alkera
# format = "1.0"
# dataframe = "polars"
# <<< alkera

import marimo

__generated_with = "0.25.1"
app = marimo.App(
    width="full",
    app_title="Revenue",
    layout_file="layouts/revenue.grid.json",
    css_file="custom.css",
    html_head_file="head.html",
    auto_download=["html", "ipynb"],
    sql_output="polars",
)

with app.setup(alkera_id="0000000001"):
    import alkera
    import polars as pl





if __name__ == "__main__":
    app.run()
