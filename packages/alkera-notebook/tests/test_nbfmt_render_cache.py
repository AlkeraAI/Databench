"""The compile memo behind ``write`` and ``analyze``: rewriting a large
notebook after editing, inserting, deleting or moving a cell reuses every
other cell's compile (each is compiled under its own id, not its place), a hit is
always what an uncached compile gives, the memo stays bounded, and nothing a
caller does to a result reaches the memo."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from alkera_notebook._marimo._ast.visitor import SQL_CALLS, register_sql_call
from alkera_notebook.format import (
    CellIR,
    NotebookIR,
    _compile,
    _fork,
    analyze,
    analyze_code,
    read,
    render_cell,
    write,
)

CORPUS = Path(__file__).resolve().parent / "format_corpus"
CASES = sorted(p for p in CORPUS.glob("v*/*") if (p / "notebook.alknb.py").is_file())
SIZE = 2000


@pytest.fixture(autouse=True)
def _empty_memo() -> Iterator[None]:
    _compile.cache_clear()
    yield
    _compile.cache_clear()


def _cell(i: int, salt: str = "") -> CellIR:
    match i % 5:
        case 0:
            # A private name: marimo mangles it with the id the cell is compiled under.
            kind, source = "python", f"import math\n_scale = {i}\nx_{i} = math.sqrt(_scale){salt}"
            meta = {}
        case 1:
            kind, source = "sql", f"SELECT * FROM t WHERE id = {i}{salt}"
            meta = {"output_var": f"df_{i}", "connection": None, "engine": None}
        case 2:
            kind, source, meta = "markdown", f"# Part {i}\n\nSee *x_{i - 2}*{salt}", {"quote": "r"}
        case 3:
            kind, source, meta = "python", f"def f_{i}(a):\n    return a + {i}{salt}", {}
        case _:
            kind, source = "python", f"for j in range(3):\n    print(j, x_{i - 4}){salt}\nx_{i - 4}"
            meta = {}
    return CellIR(
        id=f"{i:010d}",
        kind=kind,
        name="_",
        source=source,
        code=render_cell(kind, source, meta),
        meta=meta,
        resolution="keyword",
    )


def _notebook(cells: list[CellIR]) -> NotebookIR:
    return NotebookIR(cells=tuple(cells))


def _uncached(ir: NotebookIR, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Any]:
    """``write`` and ``analyze`` as they were before the memo: every cell
    compiled afresh, the writer's under its position."""

    def plain(code: str, cell_id: str, environment: object = None) -> Any:
        return _fork.compile_cell(code, cell_id=_fork.CellId(str(cell_id)))

    with monkeypatch.context() as patch:
        patch.setattr(_compile, "compile_cell", plain)
        patch.setattr(_compile, "cell_compiler", lambda ids=(): plain)
        return write(ir), analyze(ir)


def _timed(fn: Callable[[], Any]) -> float:
    started = time.perf_counter()
    fn()
    return time.perf_counter() - started


def test_a_one_cell_edit_of_a_large_notebook_reuses_every_other_compile() -> None:
    cells = [_cell(i) for i in range(SIZE)]
    cold = _timed(lambda: write(_notebook(cells)))
    warm: list[float] = []
    for attempt in range(3):
        edited = list(cells)
        edited[SIZE // 2] = _cell(SIZE // 2, salt=f"  # edit {attempt}")
        before = _compile.cache_info()
        warm.append(_timed(lambda edited=edited: write(_notebook(edited))))
        after = _compile.cache_info()
        assert after.misses - before.misses == 1
        assert after.hits - before.hits == SIZE - 1
    # The hit and miss counts above are the work the memo saves. Seconds measure
    # the runner, so they are printed, never asserted.
    print(f"render cache: cold {cold:.2f}s, warm {min(warm):.2f}s")


@pytest.mark.parametrize(
    ("edit", "misses"),
    [
        pytest.param(lambda cells: [_cell(SIZE, salt="  # new"), *cells], 1, id="insert_at_top"),
        pytest.param(lambda cells: cells[1:], 0, id="delete_the_first"),
        pytest.param(lambda cells: [cells[-1], *cells[:-1]], 0, id="move_last_to_top"),
    ],
)
def test_a_structural_edit_reuses_every_cell_it_did_not_change(
    edit: Callable[[list[CellIR]], list[CellIR]],
    misses: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cells = [_cell(i) for i in range(SIZE)]
    cold = _timed(lambda: write(_notebook(cells)))
    edited = _notebook(edit(cells))
    before = _compile.cache_info()
    elapsed = _timed(lambda: write(edited))
    after = _compile.cache_info()
    assert after.misses - before.misses == misses
    assert after.hits - before.hits == len(edited.cells) - misses
    print(f"render cache: cold {cold:.2f}s, structural edit {elapsed:.2f}s")
    assert write(edited) == _uncached(edited, monkeypatch)[0]


def test_the_graph_reuses_what_the_writer_compiled() -> None:
    notebook = _notebook([_cell(i) for i in range(200)])
    write(notebook)
    misses = _compile.cache_info().misses
    analyze(notebook)
    assert _compile.cache_info().misses == misses


def test_an_unchanged_notebook_rewrites_from_the_memo_alone() -> None:
    notebook = _notebook([_cell(i) for i in range(50)])
    first = write(notebook)
    misses = _compile.cache_info().misses
    assert write(notebook) == first
    assert _compile.cache_info().misses == misses


@pytest.mark.parametrize("case", CASES, ids=[f"{p.parent.name}/{p.name}" for p in CASES])
def test_a_memo_hit_is_what_an_uncached_compile_gives(
    case: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = (case / "notebook.alknb.py").read_text(encoding="utf-8")
    ir = read(text)
    cold = (write(ir), analyze(ir))
    hot = (write(ir), analyze(ir))
    assert _compile.cache_info().hits > 0 or not ir.cells
    reference = _uncached(ir, monkeypatch)
    assert cold == reference
    assert hot == reference


def test_cells_that_do_not_parse_are_remembered_as_not_parsing() -> None:
    code = "x = (\n"
    for _ in range(2):
        with pytest.raises(SyntaxError):
            _compile.compile_cell(code, "0000000001")
    assert _compile.cache_info().hits == 1
    graph = analyze_code([("0000000001", code), ("0000000002", "y = 1")])
    assert graph["cells"]["0000000001"]["errors"] == [{"code": "syntax_error"}]


def test_the_cell_id_is_part_of_the_key() -> None:
    # marimo names a cell's private variables after its id.
    first = _compile.compile_cell("_tmp = 1\nx = _tmp", "0000000001")
    second = _compile.compile_cell("_tmp = 1\nx = _tmp", "0000000002")
    assert _compile.cache_info().misses == 2
    assert first.cell_id != second.cell_id


@pytest.mark.parametrize(
    ("position", "mangled"),
    [
        pytest.param("0", "_cell_aaaaaaaaaa_t", id="first_cells_own_id"),
        pytest.param("1", "_cell_bbbbbbbbbb_t", id="second_cells_own_id"),
        pytest.param("2", "_cell_2_t", id="no_id_given_is_positional"),
        pytest.param("3", "_cell_3_t", id="invalid_id_is_positional"),
    ],
)
def test_the_writer_compiles_each_cell_under_its_own_id(position: str, mangled: str) -> None:
    compile_ = _compile.cell_compiler(["aaaaaaaaaa", "bbbbbbbbbb", "", "Not An Id"])
    # Swapped in this order, a result cached under one id would show it under the other.
    for other in ("0", "1", "2", "3"):
        compile_("_t = 1\nx = _t", other)
    assert compile_("_t = 1\nx = _t", position).temporaries == {mangled}


def test_a_registered_sql_call_is_not_answered_from_an_older_compile() -> None:
    code = 'rows = warehouse.query("select * from orders")'
    assert analyze_code([("0000000001", code)])["cells"]["0000000001"]["sql_tables"] == []
    register_sql_call("warehouse.query")
    try:
        tables = analyze_code([("0000000001", code)])["cells"]["0000000001"]["sql_tables"]
    finally:
        SQL_CALLS.remove("warehouse.query")
    assert tables == ["orders"]


def test_the_memo_is_bounded() -> None:
    environment = _compile.compile_environment()
    for i in range(_compile.COMPILE_CACHE_SIZE + 50):
        _compile.compile_cell(f"v = {i}", "0000000001", environment)
    assert _compile.cache_info().currsize == _compile.COMPILE_CACHE_SIZE


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda c: c.defs.add("poison"), id="defs"),
        pytest.param(lambda c: c.refs.add("poison"), id="refs"),
        pytest.param(lambda c: c.temporaries.add("poison"), id="temporaries"),
        pytest.param(lambda c: c.variable_data.clear(), id="variable_data"),
        pytest.param(lambda c: c.sql_refs.clear(), id="sql_refs"),
        pytest.param(lambda c: c.configure({"hide_code": True}), id="config"),
    ],
)
def test_changing_a_result_does_not_change_the_next_one(
    mutate: Callable[[Any], object],
) -> None:
    code = 'import duckdb\n_t = 1\nx: int = 2\ndf = mo.sql("select * from orders")'
    pristine = _compile.compile_cell(code, "0000000001")
    snapshot = (
        set(pristine.defs),
        set(pristine.refs),
        set(pristine.temporaries),
        set(pristine.variable_data),
        set(pristine.sql_refs),
        pristine.config.hide_code,
    )
    mutate(_compile.compile_cell(code, "0000000001"))
    again = _compile.compile_cell(code, "0000000001")
    assert _compile.cache_info().misses == 1
    assert (
        set(again.defs),
        set(again.refs),
        set(again.temporaries),
        set(again.variable_data),
        set(again.sql_refs),
        again.config.hide_code,
    ) == snapshot
