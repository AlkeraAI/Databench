# >>> alkera
# format = "1.0"
# reactivity = "lazy"
# dataframe = "pandas"
# env = "../envs/analytics"
# outputs_in_git = true
# autoreload = "on"
# future_key = "kept"
# list_key = [1, 2, 3]
#
# [future.table]
# nested = true
# <<< alkera

import marimo

__generated_with = "0.25.1"
app = marimo.App()

with app.setup(alkera_id="0000000001"):
    import alkera
    import polars as pl





if __name__ == "__main__":
    app.run()
