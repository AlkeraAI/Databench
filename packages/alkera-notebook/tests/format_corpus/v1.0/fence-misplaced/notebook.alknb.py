"""Doc."""
# a comment
# >>> alkera
# format = "1.0"
# reactivity = "lazy"
# <<< alkera

import marimo

__generated_with = "0.25.1"
app = marimo.App()

@app.cell(alkera_id="00000000b1")
def _():
    x = 1
    return


if __name__ == "__main__":
    app.run()
