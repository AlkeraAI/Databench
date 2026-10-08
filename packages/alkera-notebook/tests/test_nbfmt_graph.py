"""The dependency graph, compile_step and the private-name renaming."""

from __future__ import annotations

import json
import traceback
from typing import Any

import pytest
from alkera_notebook.format import analyze, analyze_code, compile_step, read
from alkera_notebook.format.mangle import MangleError, mangle_source


def cells(*codes: str) -> list[tuple[str, str]]:
    return [(f"c{i}", code) for i, code in enumerate(codes)]


# ---- analyze_code ------------------------------------------------------------------


def test_edges_run_from_definer_to_reader() -> None:
    graph = analyze_code(cells("x = 1", "y = x + 1", "print(x, y)"))
    assert graph["edges"] == [["c0", "c1"], ["c0", "c2"], ["c1", "c2"]]
    assert graph["cells"]["c2"]["refs"] == ["x", "y"]


def test_builtins_are_not_references_unless_a_cell_defines_them() -> None:
    graph = analyze_code(cells("print(len([1]))"))
    assert graph["cells"]["c0"]["refs"] == []
    shadowed = analyze_code(cells("len = 3", "print(len)"))
    assert shadowed["cells"]["c1"]["refs"] == ["len"]
    assert shadowed["edges"] == [["c0", "c1"]]


def test_private_names_stay_in_their_cell() -> None:
    graph = analyze_code(cells("_x = 1\ny = _x", "_x = 2\nz = _x"))
    assert graph["cells"]["c0"]["defs"] == ["y"]
    assert graph["cells"]["c1"]["refs"] == []
    assert graph["edges"] == []


def test_multiple_definitions_mark_every_definer() -> None:
    graph = analyze_code(cells("x = 1", "x = 2\nz = 0", "y = x", "z = 3"))
    duplicate_x = {"code": "multiple_definitions", "name": "x", "cells": ["c0", "c1"]}
    duplicate_z = {"code": "multiple_definitions", "name": "z", "cells": ["c1", "c3"]}
    assert graph["cells"]["c0"]["errors"] == [duplicate_x]
    assert graph["cells"]["c1"]["errors"] == [duplicate_x, duplicate_z]
    assert graph["cells"]["c2"]["errors"] == []
    assert graph["cells"]["c3"]["errors"] == [duplicate_z]
    assert graph["edges"] == [["c0", "c2"], ["c1", "c2"]]


def test_analyze_names_duplicates_by_cell_id() -> None:
    text = (
        "import marimo\napp = marimo.App()\n\n"
        '@app.cell(alkera_id="aaaaaaaaa1")\ndef _():\n    x = 1\n    return\n\n'
        '@app.cell(alkera_id="aaaaaaaaa2")\ndef _():\n    x = 2\n    return\n'
    )
    graph = analyze(read(text))
    expected = {"code": "multiple_definitions", "name": "x", "cells": ["aaaaaaaaa1", "aaaaaaaaa2"]}
    assert graph["cells"]["aaaaaaaaa1"]["errors"] == [expected]
    assert graph["cells"]["aaaaaaaaa2"]["errors"] == [expected]


def test_cycles_mark_their_members_only() -> None:
    graph = analyze_code(cells("a = c", "b = a", "c = b", "d = a"))
    cycle = {"code": "cycle", "cells": ["c0", "c1", "c2"]}
    assert [graph["cells"][f"c{i}"]["errors"] for i in range(4)] == [[cycle], [cycle], [cycle], []]


def test_deleting_another_cells_name_is_reported() -> None:
    graph = analyze_code(cells("x = 1", "del x"))
    assert graph["cells"]["c1"]["errors"] == [
        {"code": "delete_nonlocal", "name": "x", "cells": ["c0"]}
    ]


def test_syntax_errors_take_no_part() -> None:
    graph = analyze_code(cells("x = (", "y = x"))
    assert graph["cells"]["c0"] == {
        "defs": [],
        "refs": [],
        "sql_tables": [],
        "errors": [{"code": "syntax_error"}],
    }
    assert graph["edges"] == []


def test_sql_tables_are_listed_and_link_only_to_defined_frames() -> None:
    graph = analyze_code(
        cells(
            "orders = load()",
            'summary = alkera.sql(f"SELECT * FROM orders JOIN warehouse.people p ON true")',
        )
    )
    sql_cell = graph["cells"]["c1"]
    assert sql_cell["sql_tables"] == ["orders", "warehouse.people"]
    assert "orders" in sql_cell["refs"]
    assert "warehouse.people" not in sql_cell["refs"]
    assert ["c0", "c1"] in graph["edges"]


def test_sql_creating_a_table_links_to_its_readers() -> None:
    graph = analyze_code(
        cells("_ = mo.sql('CREATE TABLE t AS SELECT 1')", "r = mo.sql('SELECT * FROM t')")
    )
    assert graph["cells"]["c0"]["defs"] == ["t"]
    assert ["c0", "c1"] in graph["edges"]


def test_duplicate_ids_are_analyzed_once() -> None:
    graph = analyze_code([("a", "x = 1"), ("a", "x = 2")])
    assert list(graph["cells"]) == ["a"]
    assert graph["cells"]["a"]["defs"] == ["x"]


def test_the_graph_is_canonical_json() -> None:
    codes = cells("b = 1\na = 2", "print(b, a)")
    first = json.dumps(analyze_code(codes), sort_keys=True)
    assert first == json.dumps(analyze_code(list(codes)), sort_keys=True)
    assert analyze_code(codes)["cells"]["c0"]["defs"] == ["a", "b"]


def test_analyze_keeps_unparsable_cells_out_of_the_graph() -> None:
    text = (
        "import marimo\napp = marimo.App()\n\n"
        '@app.cell(alkera_id="aaaaaaaaa1")\ndef _():\n    x = 1\n    return\n\n'
        'app._unparsable_cell(r"""\ny = (\n""", name="_", alkera_id="aaaaaaaaa2")\n\n'
        'app._unparsable_cell(r"""\nprint(x)\n""", name="_", alkera_id="aaaaaaaaa3")\n'
    )
    graph = analyze(read(text))
    assert list(graph["cells"]) == ["aaaaaaaaa1", "aaaaaaaaa2", "aaaaaaaaa3"]
    assert graph["cells"]["aaaaaaaaa2"]["errors"] == [{"code": "syntax_error"}]
    assert graph["cells"]["aaaaaaaaa3"] == {"defs": [], "refs": [], "sql_tables": [], "errors": []}
    assert graph["edges"] == []


# ---- compile_step -------------------------------------------------------------------


def run_step(step: dict[str, Any], namespace: dict[str, Any]) -> object:
    exec(compile(step["body"], "<cell>", "exec"), namespace)
    if step["last_expr"] is None:
        return None
    return eval(compile(step["last_expr"], "<cell>", "eval"), namespace)


@pytest.mark.parametrize(
    ("code", "value"),
    [
        pytest.param("x = 2\nx * 3", 6, id="last-line"),
        pytest.param("x = 2; x + 1", 3, id="same-line"),
        pytest.param("x = 2\nx * 3;", None, id="semicolon"),
        pytest.param("x = 2\nx * 3  # note", 6, id="comment"),
        pytest.param("x = 5\n(x +\n    1)", 6, id="multi-line"),
        pytest.param("x = [\n  1,\n]\nlen(\n x\n)", 1, id="dangling-indent"),
        pytest.param("x = 1", None, id="no-expression"),
        pytest.param("# only a comment", None, id="comment-only"),
        pytest.param("", None, id="empty"),
        pytest.param("'é' + '☕'", "é☕", id="unicode-offsets"),
    ],
)
def test_compile_step_splits_body_and_last_expression(code: str, value: object) -> None:
    assert run_step(compile_step(code, cell_id="c1"), {}) == value


def test_line_numbers_are_kept_for_tracebacks() -> None:
    step = compile_step("a = 1\nb = 2\n1 / 0", cell_id="c1")
    try:
        run_step(step, {})
    except ZeroDivisionError as error:
        frame = traceback.extract_tb(error.__traceback__)[-1]
        assert frame.lineno == 3
    else:
        pytest.fail("expected ZeroDivisionError")


def test_private_names_are_renamed_like_marimo_does() -> None:
    namespace: dict[str, Any] = {}
    step = compile_step("_x = 2\ny = _x * 2\n_x + y", cell_id="abc")
    assert run_step(step, namespace) == 6
    assert "_x" not in namespace
    assert namespace["_cell_abc_x"] == 2
    assert step["defs"] == ["y"]


def test_without_a_cell_id_the_prefix_is_derived_from_the_code() -> None:
    first = compile_step("_x = 1\n_x")
    assert first == compile_step("_x = 1\n_x")
    assert first["body"] != compile_step("_x = 2\n_x")["body"]


def test_defs_and_refs_are_reported() -> None:
    step = compile_step("import math\ny = math.pi * r", cell_id="c")
    assert (step["defs"], step["refs"]) == (["math", "y"], ["r"])


def test_a_syntax_error_returns_the_code_with_the_error() -> None:
    assert compile_step("x = (") == {
        "body": "x = (",
        "last_expr": None,
        "defs": [],
        "refs": [],
        "error": "syntax_error",
    }


# ---- mangle_source -------------------------------------------------------------------

MANGLE_CASES = [
    pytest.param("_x = 1\nprint(_x)", ["_cell_k_x = 1", "print(_cell_k_x)"], id="name"),
    pytest.param(
        "def _f(_a):\n    return _a\n_f(1)", ["def _cell_k_f(_a):", "_cell_k_f(1)"], id="function"
    ),
    pytest.param("class _C:\n    pass\n_C()", ["class _cell_k_C:", "_cell_k_C()"], id="class"),
    pytest.param(
        "import os as _os\n_os.sep",
        ["import os as _cell_k_os", "_cell_k_os.sep"],
        id="import-alias",
    ),
    pytest.param(
        "from os import sep as _s\n_s", ["from os import sep as _cell_k_s"], id="from-alias"
    ),
    pytest.param("for _i in range(2):\n    pass", ["for _cell_k_i in range(2):"], id="for-target"),
    pytest.param("_t: int = 1", ["_cell_k_t: int = 1"], id="annotated"),
    pytest.param("[_v for _v in range(2)]", ["[_v for _v in range(2)]"], id="comprehension-local"),
    pytest.param("x = 1\nprint(x)", ["x = 1", "print(x)"], id="nothing-private"),
    pytest.param("__dunder__ = 1", ["__dunder__ = 1"], id="dunder"),
    pytest.param(
        "café_ = 1\n_é = 2\n_é", ["café_ = 1", "_cell_k_é = 2", "_cell_k_é"], id="unicode"
    ),
    pytest.param(
        "match 1:\n    case _m:\n        y = _m",
        ["    case _cell_k_m:", "        y = _cell_k_m"],
        id="match-capture",
    ),
]


@pytest.mark.parametrize(("code", "expected_lines"), MANGLE_CASES)
def test_mangle_renames_exactly_the_cell_local_names(code: str, expected_lines: list[str]) -> None:
    out = mangle_source(code, "k")
    for line in expected_lines:
        assert line in out.split("\n"), out
    assert out.count("\n") == code.count("\n")


def test_mangle_refuses_code_that_does_not_parse() -> None:
    with pytest.raises(SyntaxError):
        mangle_source("x = (", "k")


def test_mangle_error_is_an_exception() -> None:
    assert issubclass(MangleError, Exception)


AGREEMENT_CASES = {
    "global": "def f():\n    global _g\n    _g = 1\nf()\n_g",
    "nonlocal": "def f():\n    _n = 1\n    def g():\n        nonlocal _n\n        _n = 2\n    g()",
    "lambda": "_h = lambda _a: _a + 1\n_h(1)",
    "walrus": "if (_w := 3):\n    pass\n_w",
    "del": "_d = 1\ndel _d",
    "with": "import contextlib\nwith contextlib.nullcontext() as _c:\n    pass\n_c",
    "except": "try:\n    pass\nexcept Exception as _e:\n    print(_e)",
    "type-params": "def _f[_T](x: _T) -> _T:\n    return x\n_f(1)",
    "type-alias": "type _A = list[int]\nx: _A = []",
    "match-star": "match [1, 2]:\n    case [_first, *_rest]:\n        y = _rest",
    "match-mapping": "match {}:\n    case {**_kw}:\n        y = _kw",
    "class-attribute": "class K:\n    _a = 1\nK._a",
    "f-string": '_v = 1\nf"{_v} {_v!r}"',
    "dotted-import": "import os.path as _p\n_p.sep",
    "async": "async def _co():\n    async for _i in x():\n        pass",
    "decorated": "@staticmethod\ndef _deco():\n    pass",
}


@pytest.mark.parametrize("code", list(AGREEMENT_CASES.values()), ids=list(AGREEMENT_CASES))
def test_mangled_text_parses_to_marimos_renamed_tree(code: str) -> None:
    # mangle_source checks its output against marimo's renamed tree and
    # raises MangleError on any difference.
    out = mangle_source(code, "k")
    assert out.count("\n") == code.count("\n")


# ---- the runtime module ----------------------------------------------------------------


def test_the_runtime_module_is_provided_like_a_builtin() -> None:
    """The kernel binds ``alkera`` before any cell runs, so a cell using it
    without an import refers to nothing a cell has to define."""
    graph = analyze_code(cells('alkera.md("# Title")', "rows = alkera.sql('select 1')"))
    assert graph["cells"]["c0"]["refs"] == []
    assert graph["cells"]["c1"]["refs"] == []
    assert graph["cells"]["c1"]["defs"] == ["rows"]
    assert graph["edges"] == []
    assert [graph["cells"][c]["errors"] for c in ("c0", "c1")] == [[], []]


def test_a_name_that_only_looks_like_the_runtime_module_is_still_a_reference() -> None:
    graph = analyze_code(cells("alkera_client.md('x')", "y = alker"))
    assert graph["cells"]["c0"]["refs"] == ["alkera_client"]
    assert graph["cells"]["c1"]["refs"] == ["alker"]


def test_the_cell_importing_the_runtime_module_is_its_definer() -> None:
    graph = analyze_code(cells("import alkera", 'alkera.md("# Title")'))
    assert graph["cells"]["c0"]["defs"] == ["alkera"]
    assert graph["cells"]["c1"]["refs"] == ["alkera"]
    assert graph["edges"] == [["c0", "c1"]]


def test_a_cell_rebinding_the_runtime_module_is_its_definer() -> None:
    graph = analyze_code(cells("alkera = object()", "print(alkera)"))
    assert graph["cells"]["c1"]["refs"] == ["alkera"]
    assert graph["edges"] == [["c0", "c1"]]
    assert graph["cells"]["c0"]["errors"] == []


def test_two_cells_binding_the_runtime_module_are_multiple_definitions() -> None:
    graph = analyze_code(cells("import alkera", "alkera = object()", "print(alkera)"))
    duplicate = {"code": "multiple_definitions", "name": "alkera", "cells": ["c0", "c1"]}
    assert graph["cells"]["c0"]["errors"] == [duplicate]
    assert graph["cells"]["c1"]["errors"] == [duplicate]
    assert graph["cells"]["c2"]["errors"] == []
