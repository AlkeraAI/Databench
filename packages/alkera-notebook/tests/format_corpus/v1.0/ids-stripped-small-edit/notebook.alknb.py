import marimo

__generated_with = "0.25.1"
app = marimo.App()

with app.setup:
    import polars as pl


@app.cell
def _():
    df = pl.read_csv('orders.csv')
    return (df,)


@app.cell
def _(df):
    total = df['amount'].mean()
    return (total,)


@app.cell
def _(total):
    print(f'total: {total}')
    return


if __name__ == "__main__":
    app.run()
