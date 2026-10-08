import marimo

__generated_with = "0.25.1"
app = marimo.App()

with app.setup:
    import polars as pl


@app.cell
def _():
    url = 'https://example.com/data.json'
    return (url,)


@app.cell
def _(df):
    total = df['amount'].sum()
    return (total,)


@app.cell
def _(total):
    print(f'total: {total}')
    return


if __name__ == "__main__":
    app.run()
