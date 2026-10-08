"""Reader and writer behaviour beyond the corpus: robustness on any input,
editor-made notebooks, kept cells, and the writer's safe fallback."""

from __future__ import annotations

import ast
from dataclasses import replace

import pytest
from alkera_notebook.format import CellIR, NotebookIR, new_cell_id, read, render_cell, write
from alkera_notebook.format import writer as writer_module
from alkera_notebook.format.templates import sql_meta
from alkera_notebook.format.writer import file_code
from hypothesis import example, given, settings
from hypothesis import strategies as st

HEAD = 'import marimo\n\n__generated_with = "0.25.1"\napp = marimo.App()\n\n\n'
GUARD = '\n\nif __name__ == "__main__":\n    app.run()\n'


def cell(code: str, kind: str = "python", **more: object) -> CellIR:
    return CellIR(
        id=more.pop("id", new_cell_id()), kind=kind, name="_", source=code, code=code, **more
    )  # type: ignore[arg-type]


# ---- never raises, always converges ---------------------------------------------------

_fragments = st.sampled_from(
    [
        "import marimo\n",
        "app = marimo.App()\n",
        '__generated_with = "0.25.1"\n',
        "@app.cell\n",
        '@app.cell(alkera_id="a1b2c3d4e5")\n',
        "def _():\n",
        "    x = 1\n",
        "    return\n",
        "    return (x,)\n",
        "with app.setup:\n",
        "    import os\n",
        "app._unparsable_cell(r'''\n    y = (\n    ''', name=\"_\")\n",
        "# >>> alkera\n",
        '# format = "1.0"\n',
        "# <<< alkera\n",
        'if __name__ == "__main__":\n    app.run()\n',
        "\n",
        "print('stray')\n",
        "    except A, B:\n",
        "\t\n",
        '"""doc"""\n',
        "\ufeff",
        "\r\n",
    ]
)


_INDENTED_FIRST_LINE = "    x = 1\nimport marimo\napp = marimo.App()\nimport marimo\n"


@settings(max_examples=300, deadline=None)
@given(st.lists(_fragments, max_size=25).map("".join))
@example(_INDENTED_FIRST_LINE)
def test_any_notebook_like_text_reads_and_reaches_a_fixed_point(text: str) -> None:
    first = read(text)
    once = write(first)
    second = read(once)
    assert write(second) == once
    assert [c.code for c in second.cells] == [c.code for c in first.cells]
    assert not any(v.code == "internal_error" for v in first.violations)
    # Text the reader keeps as written (a header that is not Python, a cell it
    # cannot parse) stays unparsable; otherwise the written file parses.
    try:
        ast.parse(first.header_text)
    except SyntaxError:
        return
    if any(v.code == "syntax_error" for v in first.violations):
        return
    ast.parse(once)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(_INDENTED_FIRST_LINE, id="indented-header-then-stray-import"),
        pytest.param(
            "\n\n  x = 1\n" + HEAD + "@app.cell\ndef _():\n    return\n" + GUARD,
            id="blank-lines-then-indent",
        ),
    ],
)
def test_indentation_marimo_strips_does_not_break_the_written_file(text: str) -> None:
    # marimo strips the file before parsing, so an indented first line is a
    # header statement to it. Written below the settings block it must not
    # become an indented line that turns the file into a syntax error.
    first = read(text)
    assert not any(v.code == "not_a_notebook" for v in first.violations)
    once = write(first)
    ast.parse(once)
    second = read(once)
    assert not any(v.code == "not_a_notebook" for v in second.violations)
    assert second.header_text == first.header_text
    assert first.header_text.lstrip("\n") == "x = 1\n"
    assert [c.code for c in second.cells] == [c.code for c in first.cells]
    assert write(second) == once


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=300))
def test_arbitrary_text_never_raises(text: str) -> None:
    ir = read(text)
    write(ir)


@pytest.mark.parametrize("known", [None, {}, {"a1b2c3d4e5": "x = 1"}, {"bad": 1}])
def test_known_states_of_any_shape_are_accepted(known: object) -> None:
    read(HEAD + "@app.cell\ndef _():\n    x = 1\n    return\n" + GUARD, known=known)  # type: ignore[arg-type]


# ---- editor-made notebooks -------------------------------------------------------------


def test_an_editor_notebook_is_written_with_every_id_and_read_back() -> None:
    cells = [
        replace(cell("import polars as pl", "setup"), name="setup"),
        cell(render_cell("sql", "SELECT 1", sql_meta("one")), "sql", meta=sql_meta("one")),
        cell(render_cell("markdown", "# Hi", {"quote": "r"}), "markdown", meta={"quote": "r"}),
        cell("y = 2"),
    ]
    ir = NotebookIR(cells=cells)
    back = read(write(ir))
    assert [(c.id, c.kind, c.resolution) for c in back.cells] == [
        (c.id, c.kind, "keyword") for c in cells
    ]


def test_invalid_extra_and_config_values_are_not_written() -> None:
    ir = NotebookIR(
        cells=[
            cell(
                "x = 1",
                extra={
                    "alkera_ok": 1,
                    "alkera_list": [1, 2],
                    "alkera_nan": float("nan"),
                    "not_alkera": True,
                    "alkera_id": "zzzzzzzzzz",
                },
                config={"hide_code": "yes", "column": True, "disabled": True, "bogus": 1},
            )
        ]
    )
    back = read(write(ir)).cells[0]
    assert dict(back.extra) == {"alkera_ok": 1}
    assert dict(back.config) == {"disabled": True}
    assert back.id == ir.cells[0].id


def test_a_setup_cell_that_is_not_first_is_written_as_an_ordinary_cell() -> None:
    ir = NotebookIR(cells=[cell("x = 1"), replace(cell("import os", "setup"), name="setup")])
    back = read(write(ir))
    assert [c.kind for c in back.cells] == ["python", "python"]
    assert [c.code for c in back.cells] == ["x = 1", "import os"]


def test_names_that_are_not_identifiers_become_underscore() -> None:
    ir = NotebookIR(cells=[replace(cell("x = 1"), name="not an identifier")])
    assert read(write(ir)).cells[0].name == "_"


def test_the_generated_with_version_is_kept_or_defaulted() -> None:
    assert '__generated_with = "0.1.0"' in write(NotebookIR(generated_with="0.1.0"))
    assert '__generated_with = "0.25.1"' in write(NotebookIR())


# ---- kept cells ------------------------------------------------------------------------

STOCK_314 = (
    HEAD
    + "@app.cell(hide_code=True)\ndef named():\n    try:\n        pass\n"
    + "    except ValueError, TypeError:\n        pass\n    return\n"
    + GUARD
)


@pytest.mark.skipif(
    __import__("sys").version_info >= (3, 14), reason="the cell parses on Python 3.14"
)
def test_a_kept_cell_without_a_keyword_gets_its_id_inserted_in_place() -> None:
    ir = read(STOCK_314)
    kept = ir.cells[0]
    assert kept.kind == "unparsable"
    assert dict(kept.config) == {"hide_code": True}
    assert kept.name == "named"
    out = write(ir)
    assert f'@app.cell(alkera_id="{kept.id}", hide_code=True)\ndef named():' in out
    assert "    except ValueError, TypeError:\n" in out
    again = read(out)
    assert (again.cells[0].id, again.cells[0].resolution) == (kept.id, "keyword")


@pytest.mark.parametrize(
    ("verbatim", "expected"),
    [
        pytest.param(
            "@app.cell\ndef _():", '@app.cell(alkera_id="a1b2c3d4e5")\ndef _():', id="bare"
        ),
        pytest.param(
            "@app.cell()\ndef _():", '@app.cell(alkera_id="a1b2c3d4e5")\ndef _():', id="empty-call"
        ),
        pytest.param(
            "@app.cell(column=1)\ndef _():",
            '@app.cell(alkera_id="a1b2c3d4e5", column=1)\ndef _():',
            id="config",
        ),
        pytest.param(
            '@app.cell(alkera_id="0000000000")\ndef _():',
            '@app.cell(alkera_id="a1b2c3d4e5")\ndef _():',
            id="replace",
        ),
        pytest.param(
            "with app.setup:\n    x", 'with app.setup(alkera_id="a1b2c3d4e5"):\n    x', id="setup"
        ),
        pytest.param(
            "with app.setup(hide_code=True):\n    x",
            'with app.setup(alkera_id="a1b2c3d4e5", hide_code=True):\n    x',
            id="setup-call",
        ),
        pytest.param(
            'app._unparsable_cell(\n    r"""\n    x\n    """,\n    name="_"\n)',
            'app._unparsable_cell(\n    r"""\n    x\n    """,\n'
            '    name="_", alkera_id="a1b2c3d4e5"\n)',
            id="unparsable",
        ),
        pytest.param(
            'app._unparsable_cell(\n    r"""\n    x\n    """,\n)',
            'app._unparsable_cell(\n    r"""\n    x\n    """, alkera_id="a1b2c3d4e5"\n)',
            id="unparsable-trailing-comma",
        ),
        pytest.param("print('stray')", None, id="stray"),
    ],
)
def test_id_insertion_into_kept_text(verbatim: str, expected: str | None) -> None:
    assert writer_module._with_id(verbatim, "a1b2c3d4e5") == expected


def test_stale_kept_text_is_not_written_when_the_code_changed() -> None:
    ir = read(
        HEAD + 'app._unparsable_cell(r"""\nx = (\n""", name="_", alkera_id="a1b2c3d4e5")' + GUARD
    )
    edited = replace(ir.cells[0], code="x = (1", source="x = (1")
    out = write(replace(ir, cells=[edited]))
    assert "x = (1" in out
    back = read(out).cells[0]
    assert (back.id, back.code) == ("a1b2c3d4e5", "x = (1")


def test_a_kept_cell_whose_code_now_compiles_becomes_a_normal_cell() -> None:
    ir = read(
        HEAD + 'app._unparsable_cell(r"""\nx = (\n""", name="_", alkera_id="a1b2c3d4e5")' + GUARD
    )
    fixed = replace(ir.cells[0], code="x = (1)", source="x = (1)", meta={}, kind="python")
    back = read(write(replace(ir, cells=[fixed]))).cells[0]
    assert (back.id, back.kind, back.code) == ("a1b2c3d4e5", "python", "x = (1)")


# ---- the safe fallback -------------------------------------------------------------------


def test_the_writer_falls_back_to_a_form_that_keeps_every_cell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_: object, **__: object) -> None:
        raise RuntimeError("codegen failed")

    monkeypatch.setattr(writer_module._fork, "TopLevelExtraction", broken)
    ir = NotebookIR(cells=[cell("x = 1", id="a1b2c3d4e5"), cell("y = '''\n'''", id="b1b2c3d4e5")])
    out = write(ir)
    ast.parse(out)
    monkeypatch.undo()
    back = read(out)
    assert [(c.id, c.code) for c in back.cells] == [
        ("a1b2c3d4e5", "x = 1"),
        ("b1b2c3d4e5", "y = '''\n'''"),
    ]


def test_the_reader_reports_an_internal_failure_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alkera_notebook.format import reader

    def broken(*_: object, **__: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(reader, "_read", broken)
    ir = reader.read("import marimo\n")
    assert [v.code for v in ir.violations] == ["internal_error"]
    assert ir.header_text == "import marimo\n"


# ---- editor notebooks round trip ----------------------------------------------------------

_code_lines = st.sampled_from(
    [
        "import math",
        "import json  # note",
        "x = 1",
        "y = x + 1   ",
        "# a comment",
        "",
        "   ",
        "\t",
        "def f():\n    return 1  # one",
        "class K:\n    a = 1  # attr",
        "for i in range(2):\n    total = i  # loop",
        "x  # show",
        "z = 'a  # not a comment'",
    ]
)
_codes = st.lists(_code_lines, max_size=4).map("\n".join)


@settings(max_examples=300, deadline=None)
@given(st.one_of(st.none(), _codes), st.lists(_codes, max_size=5))
def test_an_editor_notebook_writes_to_a_fixed_point_without_losing_code(
    setup: str | None, codes: list[str]
) -> None:
    cells = [
        CellIR(id=f"{i:010d}", kind="python", name="_", source=c, code=c)
        for i, c in enumerate(codes)
    ]
    if setup is not None:
        cells.insert(
            0, CellIR(id="s000000000", kind="setup", name="setup", source=setup, code=setup)
        )
    ir = NotebookIR(cells=cells)
    once = write(ir)
    back = read(once)
    assert write(back) == once
    # Every cell comes back with its id, and its code up to trailing blank lines.
    assert [c.id for c in back.cells] == [c.id for c in cells]
    assert [c.code for c in back.cells] == [file_code(c.code) for c in cells]


# ---- the runtime import ---------------------------------------------------------------

SETUP_IMPORT = "with app.setup(alkera_id="


def _markdown(text: str, cell_id: str) -> CellIR:
    code = render_cell("markdown", text, {"quote": "r"})
    return CellIR(
        id=cell_id, kind="markdown", name="_", source=text, code=code, meta={"quote": "r"}
    )


def test_a_notebook_using_the_runtime_module_unimported_is_written_with_the_import() -> None:
    """The kernel binds ``alkera`` itself; the import is what lets the file
    run under plain Python or stock marimo."""
    text = write(NotebookIR(cells=[_markdown("# Title", "aaaaaaaaa1")]))
    cells = read(text).cells
    assert [(c.kind, c.code) for c in cells] == [
        ("setup", "import alkera"),
        ("markdown", render_cell("markdown", "# Title", {"quote": "r"})),
    ]
    assert cells[1].id == "aaaaaaaaa1"
    # Under plain Python the module is bound before any cell body is reached.
    assert text.index("    import alkera\n") < text.index("alkera.md(")
    # The cell no longer asks for the name as an argument nothing provides.
    assert "def _():" in text and "def _(alkera" not in text


def test_writing_the_import_is_stable() -> None:
    ir = NotebookIR(cells=[_markdown("# Title", "aaaaaaaaa1")])
    once = write(ir)
    assert write(ir) == once
    assert write(read(once)) == once
    assert once.count("import alkera\n") == 1


def test_the_import_goes_first_in_the_setup_block_the_notebook_has() -> None:
    setup = CellIR(
        id="aaaaaaaaa0", kind="setup", name="setup", source="import os", code="import os"
    )
    cells = read(write(NotebookIR(cells=[setup, _markdown("# Title", "aaaaaaaaa1")]))).cells
    assert [(c.id, c.kind, c.code) for c in cells[:1]] == [
        ("aaaaaaaaa0", "setup", "import alkera\nimport os")
    ]
    assert len(cells) == 2


@pytest.mark.parametrize(
    "codes",
    [
        pytest.param(["x = 1", "print(x)"], id="no-cell-uses-it"),
        pytest.param(["import alkera", 'alkera.md("t")'], id="a-cell-imports-it"),
        pytest.param(["alkera = 3", "print(alkera)"], id="a-cell-assigns-it"),
        pytest.param(["import alkera as ak", "ak.md('t')"], id="imported-under-another-name"),
        pytest.param(["def alkera():\n    return 1", "alkera()"], id="a-cell-defines-it"),
        pytest.param(["note = 'alkera.md is a call'"], id="named-only-in-a-string"),
    ],
)
def test_no_import_is_added(codes: list[str]) -> None:
    ir = NotebookIR(cells=[cell(code, id=f"aaaaaaaaa{i}") for i, code in enumerate(codes)])
    text = write(ir)
    assert SETUP_IMPORT not in text and "with app.setup" not in text
    assert [c.code for c in read(text).cells] == codes


def test_an_existing_setup_import_is_left_as_it_is() -> None:
    setup = CellIR(
        id="aaaaaaaaa0",
        kind="setup",
        name="setup",
        source="import os\nimport alkera",
        code="import os\nimport alkera",
    )
    cells = read(write(NotebookIR(cells=[setup, _markdown("# Title", "aaaaaaaaa1")]))).cells
    assert cells[0].code == "import os\nimport alkera"


def test_an_old_file_without_the_import_gains_it_once_and_keeps_its_cells() -> None:
    old = (
        HEAD
        + '@app.cell(alkera_id="aaaaaaaaa1")\ndef _(alkera):\n'
        + '    alkera.md(\n        r"""\n        # Title\n        """\n    )\n    return\n\n\n'
        + '@app.cell(alkera_id="aaaaaaaaa2")\ndef _():\n    x = 1\n    return\n'
        + GUARD
    )
    first = read(old)
    assert [c.id for c in first.cells] == ["aaaaaaaaa1", "aaaaaaaaa2"]
    once = write(first)
    second = read(once)
    assert [c.kind for c in second.cells] == ["setup", "markdown", "python"]
    assert [(c.id, c.code) for c in second.cells[1:]] == [(c.id, c.code) for c in first.cells]
    assert second.cells[0].code == "import alkera"
    assert write(second) == once
