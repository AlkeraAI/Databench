"""Alkera's edits to the marimo fork, one behaviour each.

Each test exercises the fork through its own API (not through the format
library), so it fails when the corresponding fenced edit in vendor/marimo is
reverted and the private package regenerated.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from alkera_notebook._marimo._ast import cell_manager
from alkera_notebook._marimo._ast.cell import CellConfig
from alkera_notebook._marimo._ast.codegen import generate_filecontents, indent_text
from alkera_notebook._marimo._ast.compiler import compile_cell
from alkera_notebook._marimo._ast.load import load_app
from alkera_notebook._marimo._ast.sql_visitor import find_sql_refs
from alkera_notebook._marimo._types.ids import CellId_t

REPO = Path(__file__).resolve().parents[3]
VENDOR = REPO / "vendor" / "marimo"


# ---- alkera-kwargs ------------------------------------------------------------------


@pytest.fixture
def marimo_log(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    """Capture the fork's logger, which does not propagate to the root."""
    logger = logging.getLogger("marimo")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING):
            yield caplog
    finally:
        logger.removeHandler(caplog.handler)


def test_alkera_keywords_are_kept_in_order_without_a_warning(
    marimo_log: pytest.LogCaptureFixture,
) -> None:
    config = CellConfig.from_dict(
        {"alkera_id": "a1b2c3d4e5", "hide_code": True, "alkera_note": "n", "alkera_x": None}
    )
    assert config.passthrough == {"alkera_id": "a1b2c3d4e5", "alkera_note": "n", "alkera_x": None}
    assert list(config.passthrough) == ["alkera_id", "alkera_note", "alkera_x"]
    assert config.hide_code is True
    assert "Invalid config keys" not in marimo_log.text


def test_other_unknown_keywords_still_warn(marimo_log: pytest.LogCaptureFixture) -> None:
    config = CellConfig.from_dict({"bogus": 1})
    assert config.passthrough == {}
    assert "Invalid config keys" in marimo_log.text


def test_codegen_writes_passthrough_keywords_first_in_every_form() -> None:
    def config(cell_id: str, **more: object) -> CellConfig:
        return CellConfig.from_dict({"alkera_id": cell_id, **more})

    text = generate_filecontents(
        ["import os", "x = 1", "def f():\n    return 1", "y = ("],
        ["setup", "_", "f", "bad"],
        [
            config("0000000001", hide_code=True),
            config("0000000002", alkera_flag=True, column=1),
            config("0000000003"),
            config("0000000004", disabled=True),
        ],
    )
    assert 'with app.setup(alkera_id="0000000001", hide_code=True):' in text
    assert '@app.cell(alkera_id="0000000002", alkera_flag=True, column=1)' in text
    assert '@app.function(alkera_id="0000000003")' in text
    assert re.search(r'name="bad", alkera_id="0000000004"\n\)', text)


def test_configure_keeps_passthrough_order() -> None:
    impl = compile_cell("x = 1", cell_id=CellId_t("c")).configure(
        CellConfig.from_dict({"alkera_id": "0000000001", "alkera_b": 1, "alkera_a": 2})
    )
    assert list(impl.config.passthrough) == ["alkera_id", "alkera_b", "alkera_a"]


# ---- sql-calls ------------------------------------------------------------------------


def test_alkera_sql_tables_are_references() -> None:
    impl = compile_cell(
        "df = alkera.sql('SELECT * FROM orders JOIN people USING (id)')",
        cell_id=CellId_t("c"),
    )
    assert {"orders", "people"} <= set(impl.refs)
    assert impl.language == "sql"


def test_the_sql_call_list_is_registrable() -> None:
    from alkera_notebook._marimo._ast import visitor

    with pytest.raises(ValueError, match=r"module\.function"):
        visitor.register_sql_call("nodot")
    visitor.register_sql_call("warehouse.query")
    try:
        impl = compile_cell("r = warehouse.query('SELECT * FROM events')", cell_id=CellId_t("c"))
        assert "events" in impl.refs
    finally:
        visitor.SQL_CALLS.remove("warehouse.query")


# ---- sqlglot-refs ---------------------------------------------------------------------

_NO_DUCKDB = """
import sys, json
sys.modules["duckdb"] = None
from alkera_notebook._marimo._ast.compiler import compile_cell
impl = compile_cell(sys.argv[1], cell_id="c")
print(json.dumps({"defs": sorted(impl.defs), "refs": sorted(impl.refs)}))
"""


def _without_duckdb(code: str) -> dict[str, list[str]]:
    import json

    result = subprocess.run(
        [sys.executable, "-c", _NO_DUCKDB, code],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)  # type: ignore[no-any-return]


def test_without_duckdb_sqlglot_finds_references_and_definitions() -> None:
    result = _without_duckdb(
        "r = mo.sql('CREATE TABLE totals AS SELECT * FROM orders; SELECT * FROM totals, people')"
    )
    assert "totals" in result["defs"]
    assert {"orders", "people"} <= set(result["refs"])
    assert "totals" not in result["refs"]


def test_without_duckdb_create_view_and_schema_are_definitions() -> None:
    result = _without_duckdb("r = mo.sql('CREATE VIEW v AS SELECT 1; CREATE SCHEMA s')")
    assert {"v", "s"} <= set(result["defs"])


def test_an_unterminated_quote_yields_no_references_instead_of_an_error() -> None:
    assert find_sql_refs('SELECT "') == set()


# ---- ids-from-host --------------------------------------------------------------------

NOTEBOOK = """import marimo

app = marimo.App()


@app.cell(alkera_id="a1b2c3d4e5")
def _():
    x = 1
    return


@app.cell(alkera_id="a1b2c3d4e5")
def _():
    y = 2
    return


@app.cell
def _():
    z = 3
    return


if __name__ == "__main__":
    app.run()
"""


def test_loaded_cells_take_their_ids_from_the_host(tmp_path: Path) -> None:
    path = tmp_path / "nb.py"
    path.write_text(NOTEBOOK, encoding="utf-8")
    cell_manager.set_default_cell_id_source(cell_manager.passthrough_cell_id("alkera_id"))
    try:
        app = load_app(str(path))
    finally:
        cell_manager.set_default_cell_id_source(None)
    assert app is not None
    ids = list(app._cell_manager.cell_ids())
    # The duplicate and the cell without a keyword fall back to the generator.
    assert ids[0] == "a1b2c3d4e5"
    assert ids[1] != "a1b2c3d4e5"
    assert len(set(ids)) == 3


def test_without_a_host_source_ids_stay_positional(tmp_path: Path) -> None:
    path = tmp_path / "nb.py"
    path.write_text(NOTEBOOK, encoding="utf-8")
    app = load_app(str(path))
    assert app is not None
    assert "a1b2c3d4e5" not in list(app._cell_manager.cell_ids())


# ---- indent-lines ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "separator",
    ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"],
    ids=["vt", "ff", "fs", "gs", "rs", "nel", "ls", "ps"],
)
def test_indent_splits_only_at_newlines(separator: str) -> None:
    assert indent_text(f"x = 'a{separator}b'\ny = 1") == f"    x = 'a{separator}b'\n    y = 1"


def test_indent_keeps_whitespace_only_lines() -> None:
    assert indent_text('s = """\n  \n"""\n\nz') == '    s = """\n      \n    """\n\n    z'


# ---- nbsp-when-needed ---------------------------------------------------------------


def test_a_non_breaking_space_inside_a_string_is_kept() -> None:
    code = "label = 'prix\u00a0: 3\u00a0€'"
    assert compile_cell(code, cell_id=CellId_t("c")).code == code


def test_a_non_breaking_space_in_code_is_still_repaired() -> None:
    impl = compile_cell("x\u00a0=\u00a01", cell_id=CellId_t("c"))
    assert impl.code == "x = 1"
    assert impl.defs == {"x"}


# ---- trailing-comment ---------------------------------------------------------------

TRAILING = """import marimo
app = marimo.App()

with app.setup:
    import math  # setup note


@app.function
def f():
    return 1  # function note


@app.class_definition
class K:
    a = 1  # class note


@app.cell
def _():
    y = 2  # cell note


@app.cell
def _():
    z = 3  # kept before a return
    return
"""


def test_a_comment_after_the_last_statement_belongs_to_the_cell() -> None:
    from alkera_notebook._marimo._ast.parse import parse_notebook

    notebook = parse_notebook(TRAILING)
    assert notebook is not None
    assert [c.code for c in notebook.cells] == [
        "import math  # setup note",
        "def f():\n    return 1  # function note",
        "class K:\n    a = 1  # class note",
        "y = 2  # cell note",
        "z = 3  # kept before a return",
    ]


def test_code_after_the_statement_is_not_taken_as_a_comment() -> None:
    from alkera_notebook._marimo._ast.parse import parse_notebook

    notebook = parse_notebook(
        "import marimo\napp = marimo.App()\n\nwith app.setup:\n    x = '#'; y = 1\n"
    )
    assert notebook is not None
    assert notebook.cells[0].code == "x = '#'; y = 1"


# ---- empty-setup ----------------------------------------------------------------------


def test_an_empty_setup_cell_with_an_id_is_written_and_read_back() -> None:
    from alkera_notebook._marimo._ast.parse import parse_notebook

    text = generate_filecontents(
        ["", "x = 1"],
        ["setup", "_"],
        [CellConfig.from_dict({"alkera_id": "0000000001"}), CellConfig()],
    )
    assert 'with app.setup(alkera_id="0000000001"):\n    pass\n' in text
    notebook = parse_notebook(text)
    assert notebook is not None
    assert [c.code for c in notebook.cells] == ["", "x = 1"]


def test_an_empty_setup_cell_without_keywords_is_still_dropped() -> None:
    text = generate_filecontents(["", "x = 1"], ["setup", "_"], [CellConfig(), CellConfig()])
    assert "app.setup" not in text


def test_a_setup_cell_that_is_only_pass_without_keywords_keeps_its_code() -> None:
    from alkera_notebook._marimo._ast.parse import parse_notebook

    notebook = parse_notebook("import marimo\napp = marimo.App()\n\nwith app.setup:\n    pass\n")
    assert notebook is not None
    assert notebook.cells[0].code == "pass"


# ---- compiler-from-host ---------------------------------------------------------------


def test_top_level_extraction_compiles_through_the_hosts_compiler() -> None:
    from alkera_notebook._marimo._ast.toplevel import TopLevelExtraction

    def compiler(code: str, cell_id: CellId_t) -> object:
        return compile_cell("def renamed():\n    return 1", cell_id=cell_id)

    hosted = TopLevelExtraction(["x = 1"], ["_"], [CellConfig()], set(), compiler=compiler)
    (status,) = hosted.statuses
    assert status.is_toplevel and status.name == "renamed"
    (plain,) = TopLevelExtraction(["x = 1"], ["_"], [CellConfig()], set()).statuses
    assert plain.defs == {"x"} and not plain.is_toplevel


# ---- sql-defs-per-cell ----------------------------------------------------------------


class _Unread:
    """A variable no cell being serialized defines; reading it is the bug."""

    annotation_data = None

    @property
    def language(self) -> str:
        raise AssertionError("another cell's variable was consulted")


def test_a_cells_function_consults_only_its_own_variables() -> None:
    from types import SimpleNamespace

    from alkera_notebook._marimo._ast.codegen import to_functiondef

    cell = compile_cell("a = 1\nb = 2", cell_id=CellId_t("0"))
    variables = {
        "a": SimpleNamespace(language="sql", annotation_data=None),
        "b": SimpleNamespace(language="python", annotation_data=None),
        **{f"other{i}": _Unread() for i in range(50)},
    }
    text = to_functiondef(cell, "_", variable_data=variables)  # type: ignore[arg-type]
    # The SQL-made def is still left out of the return; the Python one stays.
    assert text.rstrip().endswith("return (b,)")


# ---- bookkeeping ----------------------------------------------------------------------

PATCH_IDS = (
    "alkera-kwargs",
    "sql-calls",
    "sqlglot-refs",
    "ids-from-host",
    "indent-lines",
    "nbsp-when-needed",
    "trailing-comment",
    "empty-setup",
    "compiler-from-host",
    "sql-defs-per-cell",
)


def _fenced_files() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in (VENDOR / "marimo").rglob("*.py"):
        for match in re.finditer(r"# == ALKERA EDIT START (\S+)", path.read_text(encoding="utf-8")):
            found.setdefault(match.group(1), set()).add(path.relative_to(VENDOR).as_posix())
    return found


def test_every_fenced_edit_is_listed_and_tested() -> None:
    fenced = _fenced_files()
    assert set(fenced) == set(PATCH_IDS)
    readme = (VENDOR / "README.alkera.md").read_text(encoding="utf-8")
    for patch_id, files in fenced.items():
        row = next(line for line in readme.splitlines() if line.startswith(f"| `{patch_id}`"))
        for file in files:
            assert f"`{file}`" in row, (patch_id, file)


def test_every_edited_file_carries_the_notice_and_balanced_fences() -> None:
    for files in _fenced_files().values():
        for file in files:
            text = (VENDOR / file).read_text(encoding="utf-8")
            assert "Modified by Alkera" in text.split("\n", 3)[1], file
            assert text.count("# == ALKERA EDIT START") == text.count("# == ALKERA EDIT END"), file
