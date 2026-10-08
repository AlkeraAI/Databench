"""The document case catalogue: operations and outside changes through a real
``FileDocumentStore`` writing real files."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alkera_notebook.document.apply import MAX_FILE_BYTES, MAX_OPS_PER_BATCH
from alkera_notebook.document.file_store import FileDocumentStore
from alkera_notebook.document.fmt import ModuleFormat
from alkera_notebook.document.ops import (
    DeleteCell,
    EditCell,
    InsertCell,
    MoveCell,
    NotebookOp,
    NotebookOpError,
    NotebookOpsResult,
    ReplaceCell,
    RestoreCell,
    SetCellConfig,
    SetCellKind,
    SetCellName,
    SetSetting,
    TextEdit,
)
from alkera_notebook.engine.models import Actor
from freezegun import freeze_time
from pydantic import ValidationError

FMT = ModuleFormat()
ANN = Actor(kind="person", id="ann", display_name="Ann", can_edit=True, can_run=True)
BOT = Actor(kind="agent", id="bot", display_name="Agent", can_edit=True, can_run=True)

BASE = [
    ("a", "x = 1"),
    ("b", "y = x + 1"),
    ("c", "z = y * 2"),
    ("d", "w = z + x"),
]


@dataclass
class Env:
    store: FileDocumentStore
    path: str
    file: Path
    ids: dict[str, str]

    async def apply(
        self,
        *ops: NotebookOp,
        base: str | None = None,
        submit: str | None = None,
        actor: Actor = ANN,
    ) -> NotebookOpsResult:
        return await self.store.apply(self.path, list(ops), base, actor, submit)

    async def order(self) -> list[str]:
        return list((await self.store.load(self.path)).document.order)

    async def cell(self, cid: str) -> Any:
        return (await self.store.load(self.path)).document.cells[cid]

    def file_cells(self) -> list[tuple[str, str, str]]:
        ir = FMT.read(self.file.read_text(encoding="utf-8"))
        return [(c.id, c.kind, c.code) for c in ir.cells]

    def file_ids(self) -> list[str]:
        return [c[0] for c in self.file_cells()]

    def names(self, order: list[str]) -> list[str]:
        back = {v: k for k, v in self.ids.items()}
        return [back.get(i, "?") for i in order]


async def make_env(tmp_path: Path, *, setup: bool = True) -> Env:
    store = FileDocumentStore(tmp_path, fmt=FMT)
    cells = [InsertCell(kind="setup", source="import os")] if setup else []
    cells += [InsertCell(source=src) for _, src in BASE]
    stored = await store.create("nb.alknb.py", cells, {}, ANN)
    order = list(stored.document.order)
    names = (["setup"] if setup else []) + [n for n, _ in BASE]
    return Env(store, "nb.alknb.py", tmp_path / "nb.alknb.py", dict(zip(names, order, strict=True)))


async def refused(env: Env, code: str, *ops: NotebookOp) -> NotebookOpError:
    before_text = env.file.read_text()
    before_token = (await env.store.load(env.path)).token
    with pytest.raises(NotebookOpError) as info:
        await env.apply(*ops)
    assert info.value.code == code
    assert env.file.read_text() == before_text
    assert (await env.store.load(env.path)).token == before_token
    return info.value


# Cases ----------------------------------------------------------------------


async def insert_at_start(env: Env) -> None:
    r = await env.apply(InsertCell(source="v = 0", before=env.ids["a"]))
    new = r.created[0]
    assert (await env.order())[:2] == [env.ids["setup"], new]
    assert env.file_ids()[1] == new


async def insert_at_start_without_setup(tmp_path: Path) -> None:
    env = await make_env(tmp_path, setup=False)
    r = await env.apply(InsertCell(source="v = 0", before=env.ids["a"]))
    assert (await env.order())[0] == r.created[0]
    assert env.file_ids()[0] == r.created[0]


async def insert_at_end(env: Env) -> None:
    r = await env.apply(InsertCell(source="v = 0"))
    assert (await env.order())[-1] == r.created[0]
    assert env.file_cells()[-1] == (r.created[0], "python", "v = 0")


async def insert_after_deleted_cell(env: Env) -> None:
    await env.apply(DeleteCell(cell_id=env.ids["b"]))
    r = await env.apply(InsertCell(source="v = 0", after=env.ids["b"]))
    order = await env.order()
    assert order.index(r.created[0]) == order.index(env.ids["a"]) + 1
    assert order[order.index(r.created[0]) + 1] == env.ids["c"]


async def insert_before_setup(env: Env) -> None:
    await refused(env, "setup_must_be_first", InsertCell(source="v = 0", before=env.ids["setup"]))


async def insert_second_setup(env: Env) -> None:
    await refused(env, "setup_must_be_first", InsertCell(kind="setup", source="import re"))


async def edit_old_missing(env: Env) -> None:
    await refused(
        env,
        "edit_not_found",
        EditCell(cell_id=env.ids["a"], edits=[TextEdit(old="nope", new="x")]),
    )


async def edit_ambiguous(env: Env) -> None:
    await env.apply(ReplaceCell(cell_id=env.ids["d"], source="w = x + x"))
    await refused(
        env, "edit_ambiguous", EditCell(cell_id=env.ids["d"], edits=[TextEdit(old="x", new="q")])
    )


async def edit_once(env: Env) -> None:
    r = await env.apply(EditCell(cell_id=env.ids["a"], edits=[TextEdit(old="1", new="41")]))
    assert (await env.cell(env.ids["a"])).code == "x = 41"
    assert (env.ids["a"], "python", "x = 41") in env.file_cells()
    assert env.ids["a"] in r.graph.cells


async def edit_occurrence(env: Env) -> None:
    await env.apply(ReplaceCell(cell_id=env.ids["d"], source="w = x + x + x"))
    await env.apply(
        EditCell(cell_id=env.ids["d"], edits=[TextEdit(old="x", new="q", occurrence=2)])
    )
    assert (await env.cell(env.ids["d"])).source == "w = x + q + x"
    await refused(
        env,
        "edit_not_found",
        EditCell(cell_id=env.ids["d"], edits=[TextEdit(old="x", new="q", occurrence=3)]),
    )


async def edit_sequence_applies_in_order(env: Env) -> None:
    await env.apply(
        EditCell(
            cell_id=env.ids["a"],
            edits=[TextEdit(old="1", new="2"), TextEdit(old="x = 2", new="x = 3")],
        )
    )
    assert (await env.cell(env.ids["a"])).source == "x = 3"


async def replace(env: Env) -> None:
    await env.apply(ReplaceCell(cell_id=env.ids["b"], source="y = 10"))
    assert (env.ids["b"], "python", "y = 10") in env.file_cells()


async def delete_then_edit(env: Env) -> None:
    await env.apply(DeleteCell(cell_id=env.ids["b"]))
    assert env.ids["b"] not in env.file_ids()
    r = await env.apply(EditCell(cell_id=env.ids["b"], edits=[TextEdit(old="1", new="5")]))
    assert [n.kind for n in r.notices] == ["edited_deleted_cell"]
    cell = await env.cell(env.ids["b"])
    assert cell.deleted and cell.source == "y = x + 5"
    assert env.ids["b"] not in env.file_ids()


async def restore(env: Env) -> None:
    before = await env.order()
    await env.apply(DeleteCell(cell_id=env.ids["b"]))
    await env.apply(RestoreCell(cell_id=env.ids["b"]))
    assert await env.order() == before
    assert env.file_ids() == before


async def restore_after(env: Env) -> None:
    await env.apply(DeleteCell(cell_id=env.ids["b"]))
    await env.apply(RestoreCell(cell_id=env.ids["b"], after=env.ids["d"]))
    assert env.names(await env.order()) == ["setup", "a", "c", "d", "b"]


async def move_to_own_position(env: Env) -> None:
    before = await env.order()
    await env.apply(MoveCell(cell_id=env.ids["b"], after=env.ids["a"]))
    await env.apply(MoveCell(cell_id=env.ids["b"], after=env.ids["b"]))
    assert await env.order() == before


async def move_to_end(env: Env) -> None:
    await env.apply(MoveCell(cell_id=env.ids["a"]))
    assert env.names(await env.order()) == ["setup", "b", "c", "d", "a"]
    assert env.names(env.file_ids()) == ["setup", "b", "c", "d", "a"]


async def move_across_deleted_cell(env: Env) -> None:
    await env.apply(DeleteCell(cell_id=env.ids["c"]))
    await env.apply(MoveCell(cell_id=env.ids["a"], after=env.ids["c"]))
    assert env.names(await env.order()) == ["setup", "b", "a", "d"]


async def move_before_setup(env: Env) -> None:
    await refused(
        env, "setup_must_be_first", MoveCell(cell_id=env.ids["c"], before=env.ids["setup"])
    )


async def rename_to_underscore(env: Env) -> None:
    await env.apply(SetCellName(cell_id=env.ids["a"], name="load"))
    await env.apply(SetCellName(cell_id=env.ids["a"], name="_"))
    assert (await env.cell(env.ids["a"])).name == "_"


async def rename_invalid(env: Env) -> None:
    for bad in ("1abc", "class", "has space", ""):
        await refused(env, "invalid_name", SetCellName(cell_id=env.ids["a"], name=bad))


async def rename_duplicate(env: Env) -> None:
    await env.apply(SetCellName(cell_id=env.ids["a"], name="load"))
    r = await env.apply(SetCellName(cell_id=env.ids["b"], name="load"))
    assert [n.kind for n in r.notices] == ["duplicate_name"]
    assert (await env.cell(env.ids["b"])).name == "load"
    assert "def load(" in env.file.read_text()


async def set_kind_python_sql_markdown_and_back(env: Env) -> None:
    cid = env.ids["d"]
    await env.apply(ReplaceCell(cell_id=cid, source="SELECT 1"))
    await env.apply(SetCellKind(cell_id=cid, kind="sql"))
    cell = await env.cell(cid)
    assert (cell.kind, cell.source) == ("sql", "SELECT 1")
    assert "alkera.sql(" in cell.code
    assert (cid, "sql", cell.code) in env.file_cells()
    await env.apply(SetCellKind(cell_id=cid, kind="markdown"))
    cell = await env.cell(cid)
    assert (cell.kind, cell.source) == ("markdown", "SELECT 1")
    assert (cid, "markdown", cell.code) in env.file_cells()
    await env.apply(SetCellKind(cell_id=cid, kind="python"))
    cell = await env.cell(cid)
    assert cell.kind == "python" and cell.source == cell.code and "alkera.md(" in cell.code
    # Python whose code already is the SQL template becomes that SQL.
    sql_code = FMT.render_cell("sql", "SELECT 2", {"output_var": "t"})
    await env.apply(ReplaceCell(cell_id=cid, source=sql_code))
    await env.apply(SetCellKind(cell_id=cid, kind="sql"))
    cell = await env.cell(cid)
    assert (cell.kind, cell.source, cell.meta["output_var"]) == ("sql", "SELECT 2", "t")


async def set_kind_source_does_not_fit(env: Env) -> None:
    cid = env.ids["d"]
    await env.apply(ReplaceCell(cell_id=cid, source='s = """quoted"""'))
    await refused(env, "not_representable", SetCellKind(cell_id=cid, kind="markdown"))
    assert (await env.cell(cid)).kind == "python"


async def set_kind_unknown(env: Env) -> None:
    await refused(env, "unknown_kind", SetCellKind(cell_id=env.ids["a"], kind="rust"))


async def sql_edit_that_breaks_template_is_refused(env: Env) -> None:
    r = await env.apply(InsertCell(kind="sql", source="SELECT 1", meta={"connection": "pg"}))
    cid = r.created[0]
    await refused(
        env, "not_representable", EditCell(cell_id=cid, edits=[TextEdit(old="1", new='"""')])
    )
    cell = await env.cell(cid)
    assert (cell.kind, cell.source, cell.meta.get("connection")) == ("sql", "SELECT 1", "pg")


async def failing_op_refuses_batch(env: Env) -> None:
    err = await refused(
        env,
        "edit_not_found",
        InsertCell(source="v = 0"),
        DeleteCell(cell_id=env.ids["a"]),
        EditCell(cell_id=env.ids["b"], edits=[TextEdit(old="missing", new="")]),
    )
    assert err.index == 2
    assert len(await env.order()) == 5


async def unknown_cell(env: Env) -> None:
    await refused(env, "cell_not_found", DeleteCell(cell_id="zzzzzzzzzz"))


async def repeated_submit_id(env: Env) -> None:
    first = await env.apply(InsertCell(source="v = 0"), submit="batch-0001")
    again = await env.apply(InsertCell(source="v = 0"), submit="batch-0001")
    assert (first.repeat, again.repeat) == (False, True)
    assert again.created == first.created and again.token == first.token
    assert len(await env.order()) == 6
    with pytest.raises(ValueError):
        await env.apply(InsertCell(source="v = 0"), submit="bad id!")


async def stale_base_token(env: Env) -> None:
    old = (await env.store.load(env.path)).token
    await env.apply(InsertCell(source="v = 0"), base=old, actor=BOT)
    r = await env.apply(
        EditCell(cell_id=env.ids["a"], edits=[TextEdit(old="1", new="2")]), base=old
    )
    assert r.notices[0].kind == "stale_base"
    assert (await env.cell(env.ids["a"])).source == "x = 2"
    fresh = await env.apply(
        EditCell(cell_id=env.ids["a"], edits=[TextEdit(old="2", new="3")]), base=r.token
    )
    assert fresh.notices == []


async def two_hundred_ops(env: Env) -> None:
    ops: list[NotebookOp] = [InsertCell(source=f"v{i} = {i}") for i in range(200)]
    r = await env.apply(*ops)
    assert len(r.created) == 200 and len(set(r.created)) == 200
    assert env.file_ids()[-200:] == r.created


async def cap_ops_per_batch(env: Env) -> None:
    ops: list[NotebookOp] = [InsertCell(source="v = 0")] * (MAX_OPS_PER_BATCH + 1)
    err = await refused(env, "cap_exceeded", *ops)
    assert err.index == MAX_OPS_PER_BATCH


async def cap_source_size(env: Env) -> None:
    await refused(
        env, "cap_exceeded", ReplaceCell(cell_id=env.ids["a"], source="#" * ((1 << 20) + 1))
    )


async def cap_edits_per_op(env: Env) -> None:
    with pytest.raises(ValidationError):
        EditCell(cell_id=env.ids["a"], edits=[TextEdit(old="x", new="y")] * 101)


async def cap_document_size(env: Env) -> None:
    # Five 1 MB cells: the sources alone pass the 4 MiB file cap.
    big = "#" * 1_000_000
    ops: list[NotebookOp] = [InsertCell(source=big) for _ in range(5)]
    err = await refused(env, "cap_exceeded", *ops)
    assert err.index == 4


async def cap_document_size_just_under(env: Env) -> None:
    big = "#" * 1_000_000
    r = await env.apply(*[InsertCell(source=big) for _ in range(4)])
    assert len(r.created) == 4
    assert 4_000_000 < env.file.stat().st_size <= MAX_FILE_BYTES


async def cap_rendered_file_size(env: Env) -> None:
    # Short lines are indented inside the cell function in the file, so
    # sources well under the cap render to a file over it: the rendered file
    # is what is capped, not the sum of the sources.
    lines = "x\n" * 400_000
    one = await env.apply(InsertCell(source=lines))
    assert env.file.stat().st_size > 2 * len(lines)
    err = await refused(env, "cap_exceeded", InsertCell(source=lines), InsertCell(source=lines))
    assert err.index == 1
    assert one.created[0] in env.file_ids()


async def cap_cells(env: Env) -> None:
    for _ in range(3):
        await env.apply(*[InsertCell(source="pass") for _ in range(MAX_OPS_PER_BATCH)])
    await env.apply(*[InsertCell(source="pass") for _ in range(2000 - 1505)])
    await refused(env, "cap_exceeded", InsertCell(source="pass"))


async def invalid_config(env: Env) -> None:
    await refused(env, "invalid_config", SetCellConfig(cell_id=env.ids["a"], config={"x": 1}))
    await refused(
        env, "invalid_config", SetCellConfig(cell_id=env.ids["a"], config={"disabled": "yes"})
    )
    await refused(
        env, "invalid_config", SetCellConfig(cell_id=env.ids["a"], config={"column": True})
    )
    await refused(
        env, "invalid_config", InsertCell(kind="sql", source="SELECT 1", meta={"connection": "a/b"})
    )
    await refused(env, "invalid_config", SetSetting(key="reactivity", value="eager"))
    await refused(env, "invalid_config", SetSetting(key="env", value="/abs/path"))
    await refused(env, "invalid_config", SetSetting(key="color", value="red"))


async def set_config_and_settings(env: Env) -> None:
    await env.apply(SetCellConfig(cell_id=env.ids["a"], config={"disabled": True}))
    assert "disabled=True" in env.file.read_text()
    await env.apply(SetCellConfig(cell_id=env.ids["a"], config={"disabled": False}))
    assert "disabled" not in env.file.read_text()
    await env.apply(SetSetting(key="reactivity", value="lazy"), SetSetting(key="env", value="./v"))
    text = env.file.read_text()
    assert '# reactivity = "lazy"' in text and '# env = "./v"' in text
    await env.apply(SetSetting(key="header", value="# /// script\n# dependencies = []\n# ///"))
    assert env.file.read_text().startswith("# /// script")


# Outside changes --------------------------------------------------------


def rewrite(env: Env, old: str, new: str) -> None:
    text = env.file.read_text()
    assert old in text
    env.file.write_text(text.replace(old, new))


async def outside_only_file_changed(env: Env) -> None:
    rewrite(env, "x = 1", "x = 100")
    notices = await env.store.reload(env.path)
    assert notices == []
    assert (await env.cell(env.ids["a"])).code == "x = 100"


async def outside_only_memory_changed(env: Env) -> None:
    rewrite(env, "z = y * 2", "z = y * 3")
    r = await env.apply(ReplaceCell(cell_id=env.ids["b"], source="y = x + 7"))
    assert r.notices == []
    assert (await env.cell(env.ids["b"])).code == "y = x + 7"
    assert (await env.cell(env.ids["c"])).code == "z = y * 3"
    codes = dict((i, code) for i, _, code in env.file_cells())
    assert codes[env.ids["b"]] == "y = x + 7" and codes[env.ids["c"]] == "z = y * 3"


async def outside_both_changed_conflict(env: Env) -> None:
    with freeze_time("2026-10-05 12:00:00", real_asyncio=True):
        # The file changes outside, then a batch edits the same cell before the
        # store reloaded: the batch merges with the file instead of overwriting.
        rewrite(env, "x = 1", "x = 3")
        notices = (await env.apply(ReplaceCell(cell_id=env.ids["a"], source="x = 2"))).notices
    assert [n.kind for n in notices] == ["external_conflict"]
    assert notices[0].cell_id == env.ids["a"]
    saved = env.file.parent / "nb.conflict-20261005T120000.alknb.py"
    assert notices[0].data["saved_as"] == saved.name
    assert "x = 3" in saved.read_text()
    assert (await env.cell(env.ids["a"])).code == "x = 2"
    assert "x = 2" in env.file.read_text() and "x = 3" not in env.file.read_text()


async def outside_cells_added_and_removed(env: Env) -> None:
    text = env.file.read_text()
    block_b = text[text.index(f'@app.cell(alkera_id="{env.ids["b"]}")') :]
    block_b = block_b[: block_b.index("\n\n\n") + 3]
    text = text.replace(block_b, "")
    text = text.replace(
        "if __name__",
        '@app.cell(alkera_id="q0q0q0q0q0")\ndef _():\n    n = 5\n    return\n\n\nif __name__',
    )
    env.file.write_text(text)
    assert await env.store.reload(env.path) == []
    doc = (await env.store.load(env.path)).document
    assert doc.cells[env.ids["b"]].deleted
    assert doc.order[-1] == "q0q0q0q0q0" and doc.cells["q0q0q0q0q0"].code == "n = 5"


async def outside_removed_but_edited_in_memory(env: Env) -> None:
    disk = env.file.read_text()
    start = disk.index(f'@app.cell(alkera_id="{env.ids["b"]}")')
    end = disk.index("\n\n\n", start) + 3
    env.file.write_text(disk[:start] + disk[end:])
    await env.apply(ReplaceCell(cell_id=env.ids["b"], source="y = 99"))
    cell = await env.cell(env.ids["b"])
    assert not cell.deleted and cell.code == "y = 99"


async def outside_invalid_python(env: Env) -> None:
    env.file.write_text("def broken(:\n")
    notices = await env.store.reload(env.path)
    assert [n.kind for n in notices] == ["invalid_file"]
    assert env.file.read_text() == "def broken(:\n"
    assert len(await env.order()) == 5
    r = await env.apply(InsertCell(source="v = 0"))
    assert [n.kind for n in r.notices] == ["external_conflict"]
    assert (env.file.parent / r.notices[0].data["saved_as"]).read_text() == "def broken(:\n"
    assert r.created[0] in env.file_ids()


async def outside_deleted(env: Env) -> None:
    env.file.unlink()
    notices = await env.store.reload(env.path)
    assert [n.kind for n in notices] == ["file_deleted"]
    assert len(await env.order()) == 5
    assert await env.store.reload(env.path) == []
    await env.apply(InsertCell(source="v = 0"))
    assert env.file.exists()


async def outside_renamed(env: Env) -> None:
    env.file.rename(env.file.parent / "moved.alknb.py")
    notices = await env.store.reload(env.path)
    assert [n.kind for n in notices] == ["file_deleted"]
    moved = await env.store.load("moved.alknb.py")
    assert moved.document.order == await env.order()


async def path_outside_root(env: Env) -> None:
    with pytest.raises(ValueError):
        await env.store.load("../elsewhere.alknb.py")


CASES: list[Any] = [
    pytest.param(f, id=f"doc.{f.__name__}")
    for f in [
        insert_at_start,
        insert_at_end,
        insert_after_deleted_cell,
        insert_before_setup,
        insert_second_setup,
        edit_old_missing,
        edit_ambiguous,
        edit_once,
        edit_occurrence,
        edit_sequence_applies_in_order,
        replace,
        delete_then_edit,
        restore,
        restore_after,
        move_to_own_position,
        move_to_end,
        move_across_deleted_cell,
        move_before_setup,
        rename_to_underscore,
        rename_invalid,
        rename_duplicate,
        set_kind_python_sql_markdown_and_back,
        set_kind_source_does_not_fit,
        set_kind_unknown,
        sql_edit_that_breaks_template_is_refused,
        failing_op_refuses_batch,
        unknown_cell,
        repeated_submit_id,
        stale_base_token,
        two_hundred_ops,
        cap_ops_per_batch,
        cap_source_size,
        cap_edits_per_op,
        cap_document_size,
        cap_document_size_just_under,
        cap_rendered_file_size,
        cap_cells,
        invalid_config,
        set_config_and_settings,
        outside_only_file_changed,
        outside_only_memory_changed,
        outside_both_changed_conflict,
        outside_cells_added_and_removed,
        outside_removed_but_edited_in_memory,
        outside_invalid_python,
        outside_deleted,
        outside_renamed,
        path_outside_root,
    ]
]


@pytest.mark.parametrize("case", CASES)
async def test_nbeng_doc_case(tmp_path: Path, case: Callable[[Env], Awaitable[None]]) -> None:
    env = await make_env(tmp_path)
    await case(env)


async def test_nbeng_doc_insert_at_start_without_setup(tmp_path: Path) -> None:
    await insert_at_start_without_setup(tmp_path)


async def test_nbeng_doc_editing_window(tmp_path: Path) -> None:
    with freeze_time("2026-10-05 12:00:00", real_asyncio=True) as frozen:
        env = await make_env(tmp_path)
        assert await env.store.editing(env.path) == {}
        await env.apply(
            EditCell(cell_id=env.ids["b"], edits=[TextEdit(old="1", new="2")]), actor=BOT
        )
        info = await env.store.editing(env.path)
        assert list(info) == [env.ids["b"]]
        assert [(i.actor_id, i.caret) for i in info[env.ids["b"]]] == [("bot", False)]
        frozen.move_to("2026-10-05 12:00:14")
        assert list(await env.store.editing(env.path)) == [env.ids["b"]]
        frozen.move_to("2026-10-05 12:00:16")
        assert await env.store.editing(env.path) == {}


async def test_nbeng_doc_changes_stream(tmp_path: Path) -> None:
    env = await make_env(tmp_path)
    stream = env.store.changes(env.path)
    import asyncio

    nxt = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0)
    r = await env.apply(ReplaceCell(cell_id=env.ids["a"], source="x = 5"), actor=BOT)
    change = await asyncio.wait_for(nxt, 2)
    assert (change.token, change.actor_id, list(change.cell_ids), change.origin) == (
        r.token,
        "bot",
        [env.ids["a"]],
        "ops",
    )
    rewrite(env, "x = 5", "x = 6")
    await env.store.reload(env.path)
    change = await asyncio.wait_for(stream.__anext__(), 2)
    assert change.origin == "external" and list(change.cell_ids) == [env.ids["a"]]
    await env.store.close()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(stream.__anext__(), 2)


async def test_nbeng_doc_create_refuses_existing_and_readers(tmp_path: Path) -> None:
    env = await make_env(tmp_path)
    with pytest.raises(FileExistsError):
        await env.store.create(env.path, [], {}, ANN)
    reader = ANN.model_copy(update={"can_edit": False})
    from alkera_notebook.engine.errors import ForbiddenError

    with pytest.raises(ForbiddenError):
        await env.apply(InsertCell(source="v = 0"), actor=reader)


async def test_nbeng_doc_reload_unchanged_file_is_noop(tmp_path: Path) -> None:
    env = await make_env(tmp_path)
    token = (await env.store.load(env.path)).token
    assert await env.store.reload(env.path) == []
    assert (await env.store.load(env.path)).token == token


async def test_nbeng_doc_snapshot(tmp_path: Path) -> None:
    env = await make_env(tmp_path)
    await env.apply(DeleteCell(cell_id=env.ids["c"]))
    snap = await env.store.snapshot(env.path)
    assert [c for c, _ in snap.cells] == [env.ids[n] for n in ("setup", "a", "b", "d")]
    only = await env.store.snapshot(env.path, [env.ids["b"]])
    assert list(only.cells) == [(env.ids["b"], "y = x + 1")]
