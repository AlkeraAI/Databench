# >>> alkera
# format = "1.0"
# dataframe = "polars"
# <<< alkera

import marimo

__generated_with = "0.25.1"
app = marimo.App()

with app.setup(alkera_id="ye2krt48c4"):
    import alkera


@app.cell(alkera_id="00000000a4")
def _():
    doc = """
    first
       
    	last
    """
    sep = "a bcd"
    return


@app.cell(alkera_id="00000000a5")
def _(t):
    df = alkera.sql(
        rf"""
        SELECT 1
           
        FROM t  
        """,
    )
    return


@app.cell(alkera_id="00000000a6")
def _():
    alkera.md(
        r"""
        line one  
         
        line two
        """
    )
    return


if __name__ == "__main__":
    app.run()
