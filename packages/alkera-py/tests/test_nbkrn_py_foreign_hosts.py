"""``alkera`` outside the Alkera kernel, in the real runtimes: plain Python,
an IPython shell and stock marimo (from the display-library environment,
which does not have ``alkera`` installed; it is put on ``PYTHONPATH``)."""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import alkera
import pytest
from nbkrn_py_kernel_harness import rich_python

pytestmark = pytest.mark.xdist_group("nbkrn_py_foreign_hosts")

__all__ = ["rich_python"]  # a fixture, registered by import

PACKAGE_ROOT = Path(alkera.__file__).resolve().parents[1]


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "VIRTUAL_ENV"))}
    env["PYTHONPATH"] = str(PACKAGE_ROOT)
    return env


def test_a_plain_script_prints_markdown() -> None:
    code = (
        "import alkera\n"
        "alkera.output.append(alkera.md('**x** and <b>y</b>'))\n"
        "alkera.output.append(alkera.callout('careful', 'warn'))\n"
        "print(alkera._host.current().name)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=_env()
    )
    assert done.stdout == "**x** and <b>y</b>\n> **Warn**\n>\n> careful\nscript\n"


def test_a_stop_in_a_plain_script_ends_it() -> None:
    code = "import alkera\nalkera.stop(True, alkera.md('halt'))\nprint('after')\n"
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False, env=_env()
    )
    assert done.returncode == 1
    assert done.stdout == "halt\n"
    assert "StopCell" in done.stderr


IPYTHON_PROBE = """
import html
import json
from IPython.core.interactiveshell import InteractiveShell

shell = InteractiveShell.instance()
published = []
shell.display_pub.publish = lambda data, metadata=None, **kw: published.append(data)
shell.run_cell(
    "import alkera\\n"
    "alkera.output.append(alkera.md('# Title'))\\n"
    "alkera.output.replace(alkera.image(b'GIF89a....'))\\n"
    "host = alkera._host.current().name\\n"
)
print(json.dumps({"host": shell.user_ns["host"], "published": published}))
"""


def test_an_ipython_shell_renders_alkera_outputs_through_their_bundles(rich_python: str) -> None:
    done = subprocess.run(
        [rich_python, "-c", IPYTHON_PROBE], capture_output=True, text=True, check=True, env=_env()
    )
    report = json.loads(done.stdout.strip().splitlines()[-1])
    assert report["host"] == "jupyter"
    first, second = report["published"]
    assert "<h1>Title</h1>" in first["text/html"]
    assert first["text/markdown"] == "# Title"
    assert "image/gif" in second


MARIMO_NOTEBOOK = """
import marimo

app = marimo.App()


@app.cell
def _():
    import alkera

    alkera.output.append(alkera.md("# hi from **alkera**"))
    print("host is", alkera._host.current().name)
    return (alkera,)


@app.cell
def _(alkera):
    alkera.callout(alkera.md("*boxed*"), "info")
    return


@app.cell
def _(alkera):
    alkera.stop(True, alkera.md("stopped *here*"))
    print("after-" + "stop")
    return


@app.cell
def _(alkera):
    {extra}
    return


if __name__ == "__main__":
    app.run()
"""


def _export(
    rich_python: str, tmp_path: Path, extra: str = "pass"
) -> tuple[subprocess.CompletedProcess[str], str]:
    notebook = tmp_path / "nb.py"
    notebook.write_text(textwrap.dedent(MARIMO_NOTEBOOK).replace("{extra}", extra))
    out = tmp_path / "out.html"
    done = subprocess.run(
        [rich_python, "-m", "marimo", "export", "html", str(notebook), "-o", str(out)],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env=_env(),
        timeout=180,
    )
    page = out.read_text() if out.exists() else ""
    # The page embeds the session as JSON with HTML escaped for a script tag.
    for escaped, char in (("\\u003C", "<"), ("\\u003E", ">"), ("\\u0026", "&"), ('\\"', '"')):
        page = page.replace(escaped, char)
    return done, page


def test_stock_marimo_renders_alkera_outputs(rich_python: str, tmp_path: Path) -> None:
    done, page = _export(rich_python, tmp_path)
    # A clean export: alkera.stop ends its cell the way mo.stop does, not as a failure.
    assert done.returncode == 0, done.stdout + done.stderr
    assert "<h1>hi from <strong>alkera</strong></h1>" in page
    assert "host is marimo" in page
    assert "alkera-callout-info" in page and "<em>boxed</em>" in page
    assert "<p>stopped <em>here</em></p>" in page
    assert "after-stop" not in page


def test_stock_marimo_renders_an_alkera_ui_slider(rich_python: str, tmp_path: Path) -> None:
    if not (PACKAGE_ROOT / "alkera" / "ui").exists():
        pytest.skip(
            "alkera.ui is not part of this tree yet (it ships with the SQL and widgets work)"
        )
    done, page = _export(rich_python, tmp_path, extra="alkera.ui.slider(1, 10, value=4)")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "marimo-slider" in page


# --------------------------------------------------------------------------- alkera.ui under marimo

# One cell per element kind; each label is unique so its control can be found.
UI_NOTEBOOK = """
import marimo

app = marimo.App()


@app.cell
def _():
    import datetime as dt

    import alkera.ui as ui
    return dt, ui

{cells}
"""

UI_CELL = """
@app.cell
def _(dt, ui):
    {code}
    return
"""

# (id, constructor, marimo tag, expected data-* attributes)
UI_CASES = [
    ("slider", "ui.slider(1, 10, value=4, label='L-slider')", "marimo-slider",
     {"initial-value": 4, "start": 1, "stop": 10, "step": 1}),
    ("number", "ui.number(0, 20, step=2, value=6, label='L-number')", "marimo-number",
     {"initial-value": 6, "start": 0, "stop": 20, "step": 2}),
    ("text", "ui.text('hello', placeholder='name', label='L-text')", "marimo-text",
     {"initial-value": "hello", "placeholder": "name", "kind": "text"}),
    ("text_area", "ui.text_area('a\\nb', rows=3, label='L-text_area')", "marimo-text-area",
     {"initial-value": "a\nb", "rows": 3}),
    ("checkbox", "ui.checkbox(True, label='L-checkbox')", "marimo-checkbox",
     {"initial-value": True}),
    ("switch", "ui.switch(True, label='L-switch')", "marimo-switch", {"initial-value": True}),
    ("dropdown", "ui.dropdown({'One': 1, 'Two': 2}, value=2, label='L-dropdown')",
     "marimo-dropdown", {"initial-value": ["Two"], "options": ["One", "Two"],
                         "allow-select-none": False}),
    ("multiselect", "ui.multiselect(['a', 'b', 'c'], value=['c', 'a'], label='L-multiselect')",
     "marimo-multiselect", {"initial-value": ["c", "a"], "options": ["a", "b", "c"]}),
    ("radio", "ui.radio(['x', 'y'], value='y', label='L-radio')", "marimo-radio",
     {"initial-value": "y", "options": ["x", "y"]}),
    ("date", "ui.date(dt.date(2026, 3, 4), start=dt.date(2026, 1, 1), label='L-date')",
     "marimo-date", {"initial-value": "2026-03-04", "start": "2026-01-01"}),
    ("button", "ui.button('L-button')", "marimo-button", {"initial-value": 0}),
    ("run_button", "ui.run_button('L-run_button', disabled=True)", "marimo-button",
     {"initial-value": 0, "disabled": True}),
]  # fmt: skip


def _controls(page: str) -> list[tuple[str, dict[str, Any]]]:
    """Each marimo control on an exported page: its tag and its decoded
    ``data-*`` attributes."""
    found = []
    for match in re.finditer(r"<(marimo-[a-z-]+)((?:\s+data-[a-z-]+='[^']*')+)\s*>", page):
        attrs = {
            name: json.loads(html.unescape(raw))
            for name, raw in re.findall(r"data-([a-z-]+)='([^']*)'", match.group(2))
        }
        found.append((match.group(1), attrs))
    return found


@pytest.fixture(scope="module")
def ui_page(rich_python: str, tmp_path_factory: pytest.TempPathFactory) -> str:
    folder = tmp_path_factory.mktemp("marimo-ui")
    cells = "".join(UI_CELL.replace("{code}", code) for _, code, _, _ in UI_CASES)
    notebook = folder / "ui.py"
    notebook.write_text(UI_NOTEBOOK.replace("{cells}", cells))
    out = folder / "ui.html"
    done = subprocess.run(
        [rich_python, "-m", "marimo", "export", "html", str(notebook), "-o", str(out)],
        capture_output=True,
        text=True,
        check=False,
        cwd=folder,
        env=_env(),
        timeout=180,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    page = out.read_text()
    for escaped, char in (("\\u003C", "<"), ("\\u003E", ">"), ("\\u0026", "&"), ('\\"', '"')):
        page = page.replace(escaped, char)
    return page


@pytest.mark.parametrize(
    ("code", "tag", "expected"),
    [pytest.param(code, tag, expected, id=kind) for kind, code, tag, expected in UI_CASES],
)
def test_stock_marimo_shows_each_alkera_ui_element_as_its_own_control(
    ui_page: str, code: str, tag: str, expected: dict[str, Any]
) -> None:
    label = re.search(r"'(L-[a-z_]+)'", code)
    assert label is not None
    mine = [
        (found_tag, attrs)
        for found_tag, attrs in _controls(ui_page)
        if f">{label.group(1)}<" in str(attrs.get("label"))
    ]
    assert len(mine) == 1, f"no single control labelled {label.group(1)}"
    [(found_tag, attrs)] = mine
    assert found_tag == tag
    assert {k: attrs.get(k) for k in expected} == expected


# Drives marimo's own kernel: each element is built in one cell and read in
# another; a value is then set the way the browser sets it.
KERNEL_PROBE = """
import asyncio, json, threading

import alkera.ui
from marimo._ast.app_config import _AppConfig
from marimo._config.config import DEFAULT_CONFIG
from marimo._messaging.streams import (
    ThreadSafeStderr, ThreadSafeStdin, ThreadSafeStdout, ThreadSafeStream,
)
from marimo._messaging.types import KernelStreams
from marimo._runtime.commands import (
    AppMetadata, ExecuteCellCommand, UpdateUIElementCommand,
)
from marimo._runtime.kernel_lifecycle import KernelArgs, kernel_session
from marimo._session.model import SessionMode

CASES = json.loads({cases!r})


class Stream(ThreadSafeStream):
    def __init__(self, pipe=None, input_queue=None, redirect_console=False, cell_id=None):
        self.pipe, self.input_queue, self.cell_id = pipe, input_queue, cell_id
        self.redirect_console = redirect_console
        self.stream_lock = threading.Lock()

    def write(self, data):
        pass


async def main():
    stream = Stream()
    streams = KernelStreams(
        stream=stream, stdout=ThreadSafeStdout(stream),
        stderr=ThreadSafeStderr(stream), stdin=ThreadSafeStdin(stream),
    )
    metadata = AppMetadata(
        query_params={{}}, filename=None, cli_args={{}}, argv=None, app_config=_AppConfig()
    )
    args = KernelArgs(
        streams=streams, debugger=None, configs={{}}, app_metadata=metadata,
        user_config=DEFAULT_CONFIG, mode=SessionMode.EDIT, control_queue=asyncio.Queue(),
        set_ui_element_queue=asyncio.Queue(), virtual_file_storage="in_memory",
    )
    report = {{}}
    with kernel_session(args) as (k, ctx):
        run = lambda cell, code: k.run([ExecuteCellCommand(cell_id=cell, code=code)])
        await run("imports", "import datetime as dt\\nimport alkera.ui as ui\\nimport marimo as mo")
        for i, (kind, code, frontend) in enumerate(CASES):
            make = f"e{{i}} = {{code}}\\nseen{{i}} = []\\ne{{i}}.on_change(seen{{i}}.append)"
            await run(f"make{{i}}", make)
            await run(f"read{{i}}", f"out{{i}} = repr(e{{i}}.value)")
            before = k.globals[f"out{{i}}"]
            element = k.globals[f"e{{i}}"]
            await k.set_ui_element_value(
                UpdateUIElementCommand.from_ids_and_values([(element._id, frontend)]),
                notify_frontend=False,
            )
            report[kind] = {{
                "alkera": isinstance(element, getattr(alkera.ui, kind.split(":")[0])),
                "before": before,
                "after": k.globals[f"out{{i}}"],
                "seen": repr(k.globals[f"seen{{i}}"]),
            }}
        # Set from Python: marimo's own copy of the value follows.
        await run("set", "s = ui.slider(0, 10, value=1)")
        await run("assign", "s.value = 3")
        await run("marimo", "mine = mo.ui.slider.value.fget(s)\\nours = s.value")
        report["python-set"] = [k.globals["mine"], k.globals["ours"]]
    print(json.dumps(report))


asyncio.run(main())
"""

# (kind, constructor, the browser's value, the element's value after it)
ROUND_TRIPS = [
    ("slider", "ui.slider(0, 10, value=1)", 7, "7"),
    ("slider:clamped", "ui.slider(0, 10, value=1)", 99, "10"),
    ("number", "ui.number(0, 20, step=2, value=6)", 8, "8"),
    ("text", "ui.text('hi')", "bye", "'bye'"),
    ("text_area", "ui.text_area('a')", "x\ny", "'x\\ny'"),
    ("checkbox", "ui.checkbox(True)", False, "False"),
    ("switch", "ui.switch(False)", True, "True"),
    ("dropdown", "ui.dropdown({'One': 1, 'Two': 2}, value=2)", ["One"], "1"),
    ("multiselect", "ui.multiselect(['a', 'b', 'c'])", ["c", "a"], "['a', 'c']"),
    ("radio", "ui.radio(['x', 'y'], value='y')", "x", "'x'"),
    ("date", "ui.date(dt.date(2026, 3, 4))", "2026-02-02", "datetime.date(2026, 2, 2)"),
    ("button", "ui.button('Press')", 1, "1"),
    ("run_button", "ui.run_button()", 1, "1"),
]


@pytest.fixture(scope="module")
def kernel_report(rich_python: str) -> dict[str, Any]:
    cases = json.dumps([[kind, code, frontend] for kind, code, frontend, _ in ROUND_TRIPS])
    done = subprocess.run(
        [rich_python, "-c", KERNEL_PROBE.format(cases=cases)],
        capture_output=True,
        text=True,
        check=False,
        env=_env(),
        timeout=180,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    report: dict[str, Any] = json.loads(done.stdout.strip().splitlines()[-1])
    return report


@pytest.mark.parametrize(
    ("kind", "after"),
    [pytest.param(kind, after, id=kind) for kind, _, _, after in ROUND_TRIPS],
)
def test_moving_a_marimo_control_reruns_the_cells_that_read_the_alkera_element(
    kernel_report: dict[str, Any], kind: str, after: str
) -> None:
    case = kernel_report[kind]
    # The person holds an alkera.ui element ...
    assert case["alkera"] is True
    # ... the reading cell re-ran with the element's (coerced) value ...
    assert case["after"] == after
    assert case["before"] != after
    # ... and the element's own on_change heard it.
    assert case["seen"] == f"[{after}]"


def test_setting_the_value_in_python_moves_marimo_s_copy_too(
    kernel_report: dict[str, Any],
) -> None:
    assert kernel_report["python-set"] == [3, 3]
