# >>> alkera
# format = 1
# reactivity = 3
# dataframe = "arrow"
# env = "/abs/path"
# outputs_in_git = "yes"
# autoreload = true
# keep = 1
# <<< alkera
# >>> alkera
# format = "1.0"
# <<< alkera

import marimo

__generated_with = "0.25.1"
app = marimo.App()

@app.cell(alkera_id="00000000b2")
def _():
    return


if __name__ == "__main__":
    app.run()
