"""Branches of the pure op application that the case catalogue does not reach."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from alkera_notebook.document.apply import ApplyOutcome, apply_ops
from alkera_notebook.document.fmt import ModuleFormat
from alkera_notebook.document.model import Document
from alkera_notebook.document.ops import (
    DeleteCell,
    EditCell,
    InsertCell,
    MoveCell,
    NotebookOp,
    NotebookOpError,
    ReplaceCell,
    RestoreCell,
    SetCellConfig,
    SetCellKind,
    SetCellMeta,
    SetSetting,
    TextEdit,
)

FMT = ModuleFormat()
Ops = Callable[[dict[str, str]], list[NotebookOp]]


def base(setup: bool = True) -> tuple[Document, dict[str, str]]:
    ops: list[NotebookOp] = [InsertCell(kind="setup", source="import os")] if setup else []
    ops += [InsertCell(source="x = 1"), InsertCell(source="y = x"), InsertCell(source="z = y")]
    out = apply_ops(Document(), ops, fmt=FMT, new_id=FMT.new_cell_id)
    names = (["s"] if setup else []) + ["a", "b", "c"]
    return out.document, dict(zip(names, out.document.order, strict=True))


def run(ops: Ops, setup: bool = True) -> tuple[ApplyOutcome, dict[str, str]]:
    doc, ids = base(setup)
    return apply_ops(doc, ops(ids), fmt=FMT, new_id=FMT.new_cell_id), ids


REFUSED: list[Any] = [
    pytest.param(
        lambda i: [DeleteCell(cell_id=i["a"]), MoveCell(cell_id=i["a"])],
        "cell_not_found",
        id="move-deleted",
    ),
    pytest.param(
        lambda i: [InsertCell(source="v", before="zzzzzzzzzz")],
        "cell_not_found",
        id="anchor-missing",
    ),
    pytest.param(
        lambda i: [InsertCell(kind="unparsable", source="(")],
        "unknown_kind",
        id="insert-unparsable",
    ),
    pytest.param(
        lambda i: [InsertCell(kind="setup", source="import re", before=i["s"])],
        "setup_must_be_first",
        id="second-setup-first",
    ),
    pytest.param(
        lambda i: [InsertCell(kind="sql", source="S", meta={"output_var": "1x"})],
        "invalid_config",
        id="sql-output-var",
    ),
    pytest.param(
        lambda i: [InsertCell(kind="sql", source="S", meta={"show_output": "no"})],
        "invalid_config",
        id="sql-show-output",
    ),
    pytest.param(
        lambda i: [InsertCell(kind="sql", source="S", meta={"engine": 3})],
        "invalid_config",
        id="sql-engine",
    ),
    pytest.param(
        lambda i: [InsertCell(kind="sql", source="S", meta={"foo": 1})],
        "invalid_config",
        id="sql-unknown-meta",
    ),
    pytest.param(
        lambda i: [InsertCell(kind="markdown", source="M", meta={"quote": "x"})],
        "invalid_config",
        id="markdown-quote",
    ),
    pytest.param(
        lambda i: [InsertCell(source="v", meta={"a": 1})], "invalid_config", id="python-meta"
    ),
    pytest.param(
        lambda i: [EditCell(cell_id=i["a"], edits=[TextEdit(old="", new="v")])],
        "edit_ambiguous",
        id="empty-old-on-text",
    ),
    pytest.param(
        lambda i: [SetCellKind(cell_id=i["b"], kind="setup")],
        "setup_must_be_first",
        id="setup-kind-not-first",
    ),
    pytest.param(
        lambda i: [MoveCell(cell_id=i["s"], after=i["b"])],
        "setup_must_be_first",
        id="move-setup-down",
    ),
    pytest.param(
        lambda i: [SetSetting(key="header", value=3)], "invalid_config", id="header-not-text"
    ),
    pytest.param(
        lambda i: [SetSetting(key="outputs_in_git", value="yes")],
        "invalid_config",
        id="outputs-in-git-not-bool",
    ),
]


@pytest.mark.parametrize(("ops", "code"), REFUSED)
def test_nbeng_doc_apply_refused(ops: Ops, code: str) -> None:
    doc, ids = base()
    with pytest.raises(NotebookOpError) as info:
        apply_ops(doc, ops(ids), fmt=FMT, new_id=FMT.new_cell_id)
    assert info.value.code == code
    assert info.value.to_dict()["code"] == code


def test_nbeng_doc_insert_before_deleted_cell_takes_its_place() -> None:
    out, i = run(lambda i: [DeleteCell(cell_id=i["b"]), InsertCell(source="v", before=i["b"])])
    order = out.document.order
    assert order == [i["s"], i["a"], out.created[0], i["c"]]


def test_nbeng_doc_insert_unrepresentable_sql_is_refused() -> None:
    with pytest.raises(NotebookOpError) as refused:
        run(lambda i: [InsertCell(kind="sql", source='a """ b')])
    assert refused.value.code == "not_representable"


def test_nbeng_doc_empty_cell_takes_empty_old_edit() -> None:
    out, _ = run(lambda i: [InsertCell(source="")])
    cid = out.created[0]
    doc = apply_ops(
        out.document,
        [EditCell(cell_id=cid, edits=[TextEdit(old="", new="v = 1")])],
        fmt=FMT,
        new_id=FMT.new_cell_id,
    ).document
    assert doc.cells[cid].code == "v = 1"


def test_nbeng_doc_replace_deleted_cell_notices() -> None:
    out, i = run(lambda i: [DeleteCell(cell_id=i["a"]), ReplaceCell(cell_id=i["a"], source="q")])
    assert [n.kind for n in out.notices] == ["edited_deleted_cell"]
    assert out.document.cells[i["a"]].deleted


def test_nbeng_doc_noops_change_nothing() -> None:
    out, i = run(
        lambda i: [
            DeleteCell(cell_id=i["c"]),
            DeleteCell(cell_id=i["c"]),
            RestoreCell(cell_id=i["a"]),
            SetCellKind(cell_id=i["a"], kind="python"),
        ]
    )
    assert out.document.order == [i["s"], i["a"], i["b"]]
    assert out.touched == [i["c"]]


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        pytest.param(True, ["s", "a", "b", "c"], id="after-setup"),
        pytest.param(False, ["a", "b", "c"], id="first"),
    ],
)
def test_nbeng_doc_restore_first_cell(setup: bool, expected: list[str]) -> None:
    out, i = run(lambda i: [DeleteCell(cell_id=i["a"]), RestoreCell(cell_id=i["a"])], setup=setup)
    assert out.document.order == [i[n] for n in expected]


def test_nbeng_doc_first_cell_becomes_setup() -> None:
    out, i = run(lambda i: [SetCellKind(cell_id=i["a"], kind="setup")], setup=False)
    cell = out.document.cells[i["a"]]
    assert (cell.kind, cell.name) == ("setup", "setup")


def test_nbeng_doc_config_none_and_env_none_clear() -> None:
    out, i = run(
        lambda i: [
            SetCellConfig(cell_id=i["a"], config={"column": 2}),
            SetSetting(key="env", value="default"),
        ]
    )
    assert out.document.cells[i["a"]].config == {"column": 2}
    assert out.settings_changed and out.document.settings["env"] == "default"
    doc = apply_ops(
        out.document,
        [SetCellConfig(cell_id=i["a"], config={"column": None}), SetSetting(key="env", value=None)],
        fmt=FMT,
        new_id=FMT.new_cell_id,
    ).document
    assert doc.cells[i["a"]].config == {} and "env" not in doc.settings


def test_nbeng_doc_anchor_chain_without_survivor_goes_first() -> None:
    out, i = run(
        lambda i: [
            DeleteCell(cell_id=i["a"]),
            DeleteCell(cell_id=i["b"]),
            DeleteCell(cell_id=i["c"]),
        ],
        setup=False,
    )
    doc = apply_ops(
        out.document, [InsertCell(source="v", after=i["c"])], fmt=FMT, new_id=FMT.new_cell_id
    ).document
    assert len(doc.order) == 1


def test_a_minted_id_that_collides_is_never_reused() -> None:
    doc, ids = base(setup=False)
    taken = ids["a"]
    fresh = iter([taken, taken, "q0q0q0q0q0"])
    out = apply_ops(doc, [InsertCell(source="w = 9")], fmt=FMT, new_id=lambda: next(fresh))
    assert out.created == ["q0q0q0q0q0"]
    assert out.document.cells[taken].source == "x = 1"


def test_restoring_a_cell_deleted_when_it_was_first_lands_after_a_later_setup() -> None:
    doc, ids = base(setup=False)
    doc = apply_ops(doc, [DeleteCell(cell_id=ids["a"])], fmt=FMT, new_id=FMT.new_cell_id).document
    added = apply_ops(
        doc,
        [InsertCell(kind="setup", source="import os", before=ids["b"])],
        fmt=FMT,
        new_id=FMT.new_cell_id,
    )
    setup_id = added.created[0]
    restored = apply_ops(
        added.document, [RestoreCell(cell_id=ids["a"])], fmt=FMT, new_id=FMT.new_cell_id
    ).document
    assert restored.order[:2] == [setup_id, ids["a"]]


def _sql_cell() -> tuple[Document, str]:
    out = apply_ops(
        Document(),
        [InsertCell(kind="sql", source="SELECT 1", meta={"output_var": "_df"})],
        fmt=FMT,
        new_id=FMT.new_cell_id,
    )
    return out.document, out.document.order[0]


def test_nbeng_set_meta_rewrites_the_sql_cell_and_round_trips_through_the_file() -> None:
    doc, q = _sql_cell()
    out = apply_ops(
        doc,
        [SetCellMeta(cell_id=q, meta={"connection": "warehouse", "output_var": "orders"})],
        fmt=FMT,
        new_id=FMT.new_cell_id,
    )
    cell = out.document.cells[q]
    assert cell.code == (
        'orders = alkera.sql(\n    rf"""\n    SELECT 1\n    """,\n    connection="warehouse",\n)'
    )
    # Reading the code back gives the meta the document holds.
    kind, source, meta = FMT.classify(cell.code)
    assert (kind, source) == ("sql", "SELECT 1")
    assert meta["connection"] == "warehouse" and meta["output_var"] == "orders"
    assert cell.meta["connection"] == "warehouse"


def test_nbeng_set_meta_null_returns_a_key_to_its_default() -> None:
    doc, q = _sql_cell()
    doc = apply_ops(
        doc,
        [SetCellMeta(cell_id=q, meta={"connection": "warehouse", "show_output": False})],
        fmt=FMT,
        new_id=FMT.new_cell_id,
    ).document
    out = apply_ops(
        doc,
        [SetCellMeta(cell_id=q, meta={"connection": None, "show_output": None})],
        fmt=FMT,
        new_id=FMT.new_cell_id,
    )
    cell = out.document.cells[q]
    assert cell.code == '_df = alkera.sql(\n    rf"""\n    SELECT 1\n    """,\n)'
    assert cell.meta.get("connection") is None


@pytest.mark.parametrize(
    "meta",
    [
        pytest.param({"output_var": "1x"}, id="result-name"),
        pytest.param({"connection": 'wh"'}, id="connection-quote"),
        pytest.param({"show_output": "no"}, id="show-output"),
        pytest.param({"quote": "r"}, id="markdown-key-on-sql"),
    ],
)
def test_nbeng_set_meta_refuses_what_sql_does_not_admit(meta: dict[str, Any]) -> None:
    doc, q = _sql_cell()
    with pytest.raises(NotebookOpError) as refused:
        apply_ops(doc, [SetCellMeta(cell_id=q, meta=meta)], fmt=FMT, new_id=FMT.new_cell_id)
    assert refused.value.code == "invalid_config"


def test_nbeng_set_meta_refuses_a_python_cell() -> None:
    doc, ids = base(setup=False)
    with pytest.raises(NotebookOpError) as refused:
        apply_ops(
            doc,
            [SetCellMeta(cell_id=ids["a"], meta={"connection": "wh"})],
            fmt=FMT,
            new_id=FMT.new_cell_id,
        )
    assert refused.value.code == "invalid_config"
