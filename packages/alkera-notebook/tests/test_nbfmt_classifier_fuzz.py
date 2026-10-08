"""SQL and Markdown templates: exact round trips and exact refusals."""

from __future__ import annotations

from typing import Any

import pytest
from alkera_notebook.format import classify, read, render_cell, write
from alkera_notebook.format.ir import CellIR, NotebookIR
from alkera_notebook.format.templates import python_string, sql_meta
from hypothesis import assume, given, settings
from hypothesis import strategies as st

_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\r"),
    max_size=80,
)
_bodies = _text.filter(lambda s: '"""' not in s)
_identifiers = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,12}", fullmatch=True).filter(
    lambda s: (
        s not in {"None", "True", "False", "def", "class", "if", "for", "in", "is", "or"}
        and s.isidentifier()
        and __import__("keyword").iskeyword(s) is False
    )
)
_connections = st.none() | st.text(max_size=20).filter(lambda s: "\r" not in s)


@settings(max_examples=400, deadline=None)
@given(_bodies, _identifiers, _connections, st.booleans())
def test_sql_bodies_round_trip_exactly(
    source: str, var: str, connection: str | None, show_output: bool
) -> None:
    meta = sql_meta(var, connection, show_output)
    code = render_cell("sql", source, meta)
    assert classify(code) == ("sql", source, meta)


@settings(max_examples=400, deadline=None)
@given(_bodies, st.sampled_from(["r", "rf"]))
def test_markdown_bodies_round_trip_exactly(source: str, quote: str) -> None:
    code = render_cell("markdown", source, {"quote": quote})
    assert classify(code) == ("markdown", source, {"quote": quote})


@settings(max_examples=300, deadline=None)
@given(st.tuples(_text, _text).map(lambda p: p[0] + '"""' + p[1]))
def test_text_with_triple_quotes_cannot_be_represented(source: str) -> None:
    for kind, meta in (("sql", sql_meta("df")), ("markdown", {"quote": "r"})):
        code = render_cell(kind, source, meta)
        assert code == source
        assert classify(code)[0] != kind or classify(code)[1] != source


def _mutations(code: str) -> list[str]:
    """Near misses: every single-character insertion of a space or quote,
    deletion, or line-ending change that is not a template rendering."""
    out = []
    for i in range(len(code) + 1):
        out.append(code[:i] + " " + code[i:])
        if i < len(code):
            out.append(code[:i] + code[i + 1 :])
    out.append(code + "\n")
    out.append(code.replace("\n", "\r\n"))
    out.append(code.replace("    ", "  ", 1))
    out.append(code.replace("alkera.", "mo.", 1))
    return out


@settings(max_examples=60, deadline=None)
@given(_bodies.filter(lambda s: len(s) < 20), st.sampled_from(["sql", "markdown"]))
def test_near_misses_stay_python_with_their_bytes(source: str, kind: str) -> None:
    meta: dict[str, Any] = sql_meta("df", "W", True) if kind == "sql" else {"quote": "r"}
    code = render_cell(kind, source, meta)
    for mutated in _mutations(code):
        got_kind, got_source, got_meta = classify(mutated)
        if got_kind == "python":
            assert (got_source, got_meta) == (mutated, {})
        else:
            # A mutation can land on another valid rendering (a space added
            # inside the text); it must then round-trip exactly.
            assert render_cell(got_kind, got_source, got_meta) == mutated


@pytest.mark.parametrize(
    "code",
    [
        pytest.param('df = alkera.sql(\n    f"""\n    SELECT 1\n    """,\n)', id="plain-f-string"),
        pytest.param('df = alkera.sql(\n    rf"""\n    SELECT 1\n    """\n)', id="no-comma"),
        pytest.param(
            'df = alkera.sql(\n    rf"""\n    SELECT 1\n    """,\n    engine=e,\n)', id="engine"
        ),
        pytest.param(
            'df = alkera.sql(\n    rf"""\n    SELECT 1\n    """,\n    output=True,\n)',
            id="output-true",
        ),
        pytest.param(
            'df = alkera.sql(\n    rf"""\n    SELECT 1\n    """,\n'
            '    output=False,\n    connection="W",\n)',
            id="options-out-of-order",
        ),
        pytest.param(
            'df = alkera.sql(\n    rf"""\n    SELECT 1\n    """,\n    connection=name,\n)',
            id="connection-not-literal",
        ),
        pytest.param("df = alkera.sql(\n    rf'''\n    SELECT 1\n    ''',\n)", id="single-quotes"),
        pytest.param('df = mo.sql(\n    rf"""\n    SELECT 1\n    """,\n)', id="mo-sql"),
        pytest.param('class = alkera.sql(\n    rf"""\n    SELECT 1\n    """,\n)', id="keyword-var"),
        pytest.param('df = alkera.sql(\n    rf"""\n  SELECT 1\n    """,\n)', id="under-indented"),
        pytest.param(
            'df = alkera.sql(\n    rf"""\n    SELECT 1\n    """,\n)\nprint(df)', id="trailing-code"
        ),
        pytest.param('alkera.md(\n    """\n    # x\n    """\n)', id="md-no-prefix"),
        pytest.param('alkera.md(\n    f"""\n    # x\n    """\n)', id="md-f-only"),
        pytest.param('alkera.md(\n    r"""\n    # x\n    """,\n)', id="md-trailing-comma"),
        pytest.param('alkera.md(r"""# x""")', id="md-one-line"),
    ],
)
def test_deviations_from_the_template_are_python(code: str) -> None:
    assert classify(code) == ("python", code, {})


def test_sql_meta_is_reported() -> None:
    code = (
        'orders = alkera.sql(\n    rf"""\n    SELECT 1\n    """,\n'
        '    connection="Ware house-1",\n    output=False,\n)'
    )
    assert classify(code) == (
        "sql",
        "SELECT 1",
        {
            "output_var": "orders",
            "connection": "Ware house-1",
            "engine": None,
            "show_output": False,
        },
    )


@pytest.mark.parametrize(
    ("value", "literal"),
    [
        pytest.param("plain", '"plain"', id="double-quoted"),
        pytest.param("it's", '"it\'s"', id="apostrophe"),
        pytest.param('say "hi"', "'say \"hi\"'", id="double-quotes-inside"),
        pytest.param("both ' and \"", "'both \\' and \"'", id="both"),
        pytest.param("back\\slash", '"back\\\\slash"', id="backslash"),
        pytest.param("tab\there", '"tab\\there"', id="control"),
    ],
)
def test_python_string_prefers_double_quotes_and_round_trips(value: str, literal: str) -> None:
    assert python_string(value) == literal
    assert eval(literal) == value


@pytest.mark.parametrize("kind", ["python", "setup", "function", "class", "unparsable", "nope"])
def test_other_kinds_render_their_source_as_code(kind: str) -> None:
    assert render_cell(kind, "x = 1", {}) == "x = 1"


@settings(max_examples=40, deadline=None)
@given(_bodies, _identifiers)
def test_sql_cells_survive_a_file_round_trip(source: str, var: str) -> None:
    meta = sql_meta(var)
    code = render_cell("sql", source, meta)
    try:
        compile(code, "<cell>", "exec")
    except SyntaxError:
        assume(False)
    # A notebook as one is made: the setup block imports the runtime module.
    setup = CellIR(
        id="a1b2c3d4e4", kind="setup", name="setup", source="import alkera", code="import alkera"
    )
    ir = NotebookIR(
        cells=[
            setup,
            CellIR(id="a1b2c3d4e5", kind="sql", name="_", source=source, code=code, meta=meta),
        ]
    )
    back = read(write(ir)).cells
    assert [(cell.id, cell.kind, cell.source) for cell in back] == [
        ("a1b2c3d4e4", "setup", "import alkera"),
        ("a1b2c3d4e5", "sql", source),
    ]
