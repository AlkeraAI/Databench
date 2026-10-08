# alkera

The public Python API for Alkera notebooks and data tests. Standard library only, Python 3.8 and later.

```python
import alkera
```

## Where it runs

The same calls work in four places. `alkera` picks the host when a call runs.

| Call | Alkera kernel | Stock marimo | Jupyter or IPython | Plain Python |
| --- | --- | --- | --- | --- |
| Output objects (`md`, `html`, `image`, `hstack`, `vstack`, `callout`) | rendered | rendered (`_mime_`) | rendered (`_repr_mimebundle_`) | printed as text |
| `output.append`, `replace`, `clear` | the running cell's output | `marimo.output` | `IPython.display` | printed; `clear` does nothing |
| `stop` | the cell ends as stopped | as `mo.stop` | raises `alkera.StopCell` | raises `alkera.StopCell` |
| `status.progress_bar`, `status.spinner` | live in the cell | live in the cell | live in the cell | printed |
| `persistent_cache` | yes | yes | yes | yes |
| `widget` | reruns cells that read it | no effect | no effect | no effect |
| `args` | the run's arguments | `mo.cli_args()` | empty | empty |
| `call` | reaches the server the kernel runs for | raises `ServiceUnavailable` | raises `ServiceUnavailable` | raises `ServiceUnavailable` |
| `test`, `Connection`, `RowSet` | yes | yes | yes | yes |

Nothing is fetched from the network. Every output is self-contained HTML with inline styles, and images travel as `data:` URLs.

## Output objects

Each object renders by itself in any front end that speaks the IPython display protocol or marimo's, so it works as a cell's last expression with no Alkera code involved.

### `alkera.md(text)`

Markdown rendered to HTML: headings, emphasis, strikethrough, code spans, fenced code, lists, block quotes, rules, links and images. Raw HTML in the text is escaped, and a link whose URL is not `http`, `https`, `mailto` or relative is shown as text. Indentation common to every line is removed, so triple-quoted text can be indented with the code.

```python
alkera.md("""
    # Orders

    **12** orders failed validation. See [the runbook](https://example.com/runbook).
""")
```

### `alkera.html(text)`

HTML shown as is.

```python
alkera.html("<table><tr><td>42</td></tr></table>")
```

### `alkera.image(data, *, mimetype=None)`

An image from bytes or a file path. PNG, JPEG, GIF, WebP and SVG are recognised from their content; pass `mimetype` for anything else that is one of those. SVG is shown through an `<img>` tag, so scripts inside it never run.

```python
alkera.image("chart.png")
```

### `alkera.hstack(items)` and `alkera.vstack(items)`

Items side by side (wrapping when narrow) or one above the other. Items can be output objects, text (rendered as Markdown) or any value a notebook can display. `gap` sets the space between them in `rem`.

```python
alkera.hstack([alkera.md("**Before**"), alkera.md("**After**")], gap=1)
```

### `alkera.callout(obj, kind)`

`obj` in a coloured box. `kind` is `neutral`, `info`, `success`, `warn` or `danger`.

```python
alkera.callout("The warehouse is read-only today.", "warn")
```

## The cell's output

### `alkera.output.append(obj)`, `alkera.output.replace(obj)`, `alkera.output.clear()`

Add to, replace or clear the running cell's output from anywhere in the cell, including a thread the cell started.

```python
alkera.output.append(alkera.md("Loading orders"))
alkera.output.replace(alkera.md("Loaded 1,204 orders"))
```

### `alkera.stop(predicate, output=None)`

When `predicate` is true, end the cell here and show `output`. The cell counts as stopped, not failed, and the cells that depend on it do not run.

```python
alkera.stop(not connection_name, alkera.md("Choose a connection to continue."))
```

### `alkera.status.progress_bar(iterable, *, title=None, total=None)`

Iterate while showing progress as the cell's output. The bar replaces the whole output at most ten times a second and once more at the end. `total` defaults to `len(iterable)` when it has one.

```python
for path in alkera.status.progress_bar(paths, title="Parsing"):
    parse(path)
```

### `alkera.status.spinner(title)`

A spinner shown while the block runs, cleared when it ends. `update(title)` changes its text.

```python
with alkera.status.spinner("Fetching") as spin:
    rows = fetch()
    spin.update("Parsing")
    frame = parse(rows)
```

## Caching, widgets and the service

### `alkera.persistent_cache`

A decorator that keeps results on disk across kernel restarts, under `__marimo__/cache/alkera/` in the notebook's directory. The key is the `sha256` of the function's source and its pickled arguments, so editing the function or calling it with new arguments computes again. A damaged entry is recomputed; writes are atomic. An argument or result that cannot be pickled runs without the cache, with a warning. `directory=` puts the entries elsewhere. Entries are pickles, so keep them in a directory only you can write.

```python
@alkera.persistent_cache
def daily_totals(day: str) -> dict[str, float]:
    return expensive_query(day)
```

### `alkera.widget(obj)`

Make an ipywidget or anywidget reactive: when its value changes in the notebook, the cells that read it run again. Returns `obj`.

```python
import ipywidgets
threshold = alkera.widget(ipywidgets.IntSlider(value=10, max=100))
```

### `alkera.args()`

The arguments the notebook was run with, as a read-only mapping. `alkera-notebook run report.py -- --region eu` gives `{"region": "eu"}`; it is empty in the editor.

```python
region = alkera.args().get("region", "us")
```

### `alkera.call(name, /, **params)`

Call a method of the server the kernel runs for, attributed to the running cell. Outside the Alkera kernel it raises `alkera.ServiceUnavailable`.

```python
job = alkera.call("jobs.start", notebook="report.py")
```

### `alkera.sql`, `alkera.ui`, `alkera.chart`

SQL, notebook widgets and charts load on first use. An installation without one raises `ImportError` naming it.

```python
try:
    from alkera import chart
except ImportError:
    chart = None
```

## Data tests

### `alkera.test(*, refs, severity="blocking", enabled=True, name=None)`

Register a pytest-native data test bound to the URNs it reads. The function is returned unchanged, so plain pytest runs it. `conn.sql` returns an `alkera.RowSet`; `{orders}` names the relation bound through `refs`.

```python
@alkera.test(refs=["duckdb://shop/main.orders#amount"])
def test_amounts_are_positive(conn: alkera.Connection) -> None:
    bad = conn.sql("select * from {orders} where amount < 0")
    assert bad.is_empty(), bad.sample(5)
```

`alkera.registered_tests()` lists what is registered and `alkera.reset_registered()` clears it.

## Errors

| Error | Raised when |
| --- | --- |
| `alkera.ConnectionNotConfigured` | `alkera.sql` names a connection this environment has no configuration for |
| `alkera.ServiceUnavailable` | `alkera.call` runs outside the Alkera kernel |
| `alkera.StopCell` | `alkera.stop` ends a cell outside the Alkera kernel and marimo |
| `alkera.FeatureMissing` | an optional part (`sql`, `ui`, `chart`) is not installed; an `ImportError` |
