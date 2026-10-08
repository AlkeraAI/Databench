import marimo as mo

__generated_with = "0.20.0"
app = mo.App(width="medium")

with app.setup(hide_code=True):
    import asyncio


@app.cell
async def _():
    await asyncio.sleep(0)
    return


@app.cell
def load_data():
    data = [1, 2, 3]
    return (data,)


@app.function
def helper():
    return 1


if __name__ == "__main__":
    app.run()
