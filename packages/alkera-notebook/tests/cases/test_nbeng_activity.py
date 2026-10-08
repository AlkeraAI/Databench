"""The activity feed: who did what to which cells, and what it left stale.

The pure core (what a batch changed) is driven with documents in and changes
out; the feed itself through a real engine, the file store and (where a cell
runs) the stand-in kernel; the agent's view of it through the engine-backed
tool port and the digest.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from alkera_notebook.digest import EPOCH, render_notebook
from alkera_notebook.document.apply import apply_ops
from alkera_notebook.document.fmt import ModuleFormat
from alkera_notebook.document.model import Document
from alkera_notebook.document.ops import (
    DeleteCell,
    EditCell,
    InsertCell,
    MoveCell,
    NotebookOp,
    ReplaceCell,
    RestoreCell,
    SetCellConfig,
    SetCellKind,
    SetCellName,
    SetSetting,
    TextEdit,
)
from alkera_notebook.engine import (
    ActivityEntry,
    CellsTarget,
    EnvAction,
    NotebookClient,
    SettingsChange,
)
from alkera_notebook.engine.activity import batch_changes
from alkera_notebook.envs.static import StaticEnvRegistry
from alkera_notebook.tools import ActorRef
from alkera_notebook.tools.engine_adapter import EngineWorkspace
from nbeng_fakes import ManualClock
from nbeng_harness import ANN, BOB, engine_for, notebook, run_cells, until

FMT = ModuleFormat()

# The pure core -----------------------------------------------------------------

_counter = iter(range(10_000))


def _new_id() -> str:
    return f"c{next(_counter):09d}"


def _base() -> tuple[Document, list[str]]:
    """Four live cells a..d and a deleted cell e (after d)."""
    out = apply_ops(
        Document(),
        [InsertCell(source=s) for s in ("x = 1", "y = x", "z = y", "w = z", "v = 0")],
        fmt=FMT,
        new_id=_new_id,
    )
    ids = list(out.document.order)
    doc = apply_ops(out.document, [DeleteCell(cell_id=ids[4])], fmt=FMT, new_id=_new_id).document
    return doc, ids


def _changes(ops: Callable[[list[str]], Sequence[NotebookOp]]) -> tuple[Any, list[str], list[str]]:
    before, ids = _base()
    batch = list(ops(ids))
    out = apply_ops(before, batch, fmt=FMT, new_id=_new_id)
    return batch_changes(before, out.document, batch, out.created), ids, out.created


L = Callable[[list[str]], Sequence[NotebookOp]]


@pytest.mark.parametrize(
    ("ops", "expected"),
    [
        pytest.param(
            lambda i: [ReplaceCell(cell_id=i[1], source="y = 2")], [("edit", [1])], id="replace"
        ),
        pytest.param(
            lambda i: [EditCell(cell_id=i[2], edits=[TextEdit(old="y", new="x")])],
            [("edit", [2])],
            id="edit",
        ),
        pytest.param(lambda i: [DeleteCell(cell_id=i[0])], [("delete", [0])], id="delete"),
        pytest.param(lambda i: [RestoreCell(cell_id=i[4])], [("restore", [4])], id="restore"),
        pytest.param(lambda i: [MoveCell(cell_id=i[3], before=i[0])], [("move", [3])], id="move"),
        pytest.param(
            lambda i: [SetCellName(cell_id=i[0], name="load")], [("rename", [0])], id="rename"
        ),
        pytest.param(
            lambda i: [SetCellKind(cell_id=i[0], kind="markdown")], [("kind", [0])], id="kind"
        ),
        pytest.param(
            lambda i: [SetCellConfig(cell_id=i[0], config={"hide_code": True})],
            [("config", [0])],
            id="config",
        ),
        pytest.param(
            lambda i: [
                ReplaceCell(cell_id=i[1], source="y = 3"),
                DeleteCell(cell_id=i[3]),
                ReplaceCell(cell_id=i[2], source="z = 3"),
                ReplaceCell(cell_id=i[1], source="y = 4"),
            ],
            [("edit", [1, 2]), ("delete", [3])],
            id="grouped_by_change_in_batch_order",
        ),
    ],
)
def test_batch_names_only_the_cells_it_changed(
    ops: L, expected: list[tuple[str, list[int]]]
) -> None:
    changes, ids, _ = _changes(ops)
    assert changes.cells == [(c, [ids[n] for n in idx]) for c, idx in expected]
    assert changes.settings == [] and not changes.env_changed


@pytest.mark.parametrize(
    "ops",
    [
        pytest.param(lambda i: [ReplaceCell(cell_id=i[0], source="x = 1")], id="same_source"),
        pytest.param(lambda i: [DeleteCell(cell_id=i[4])], id="delete_deleted"),
        pytest.param(lambda i: [RestoreCell(cell_id=i[0])], id="restore_live"),
        pytest.param(lambda i: [MoveCell(cell_id=i[1], after=i[0])], id="move_in_place"),
        pytest.param(lambda i: [MoveCell(cell_id=i[1], after=i[1])], id="move_onto_itself"),
        pytest.param(lambda i: [SetCellName(cell_id=i[0], name="_")], id="same_name"),
        pytest.param(lambda i: [SetCellKind(cell_id=i[0], kind="python")], id="same_kind"),
        pytest.param(
            lambda i: [SetCellConfig(cell_id=i[0], config={"hide_code": False})],
            id="default_config",
        ),
        pytest.param(lambda i: [SetSetting(key="reactivity", value="autorun")], id="same_setting"),
        pytest.param(lambda i: [SetSetting(key="header", value="")], id="same_header"),
    ],
)
def test_an_op_that_changes_nothing_names_nothing(ops: L) -> None:
    changes, _, _ = _changes(ops)
    assert (changes.cells, changes.settings, changes.env_changed) == ([], [], False)


def test_inserts_name_the_created_cells_and_cells_they_shift_are_not_moved() -> None:
    changes, ids, created = _changes(
        lambda i: [InsertCell(source="a = 0", before=i[0]), InsertCell(source="b = 0")]
    )
    assert changes.cells == [("insert", created)]
    assert len(created) == 2 and not set(created) & set(ids)


def test_a_move_among_inserts_is_still_a_move() -> None:
    changes, ids, created = _changes(
        lambda i: [InsertCell(source="a = 0", after=i[0]), MoveCell(cell_id=i[2], after=i[3])]
    )
    assert changes.cells == [("insert", created), ("move", [ids[2]])]


def test_cells_the_ops_do_not_name_are_never_listed() -> None:
    before, ids = _base()
    batch = [ReplaceCell(cell_id=ids[0], source="x = 5")]
    after = apply_ops(before, batch, fmt=FMT, new_id=_new_id).document
    # Someone else's change landed in the same window.
    after.cells[ids[3]].source = after.cells[ids[3]].code = "w = 99"
    after.cells[ids[2]].name = "theirs"
    assert batch_changes(before, after, batch, []).cells == [("edit", [ids[0]])]


@pytest.mark.parametrize(
    ("key", "value", "settings", "env"),
    [
        pytest.param("reactivity", "lazy", ["reactivity"], False, id="reactivity"),
        pytest.param("outputs_in_git", True, ["outputs_in_git"], False, id="outputs_in_git"),
        pytest.param("header", "# hello", ["header"], False, id="header"),
        pytest.param("env", "./venv", [], True, id="env"),
    ],
)
def test_settings_and_env_changes(key: str, value: Any, settings: list[str], env: bool) -> None:
    changes, _, _ = _changes(lambda i: [SetSetting(key=key, value=value)])
    assert (changes.cells, changes.settings, changes.env_changed) == ([], settings, env)


# The feed through the engine ------------------------------------------------------


def _shape(entries: Sequence[ActivityEntry]) -> list[tuple[str, str | None, list[str], str]]:
    return [(e.kind, e.change, e.cell_ids, e.actor.id) for e in entries]


async def _since_start(client: NotebookClient) -> list[ActivityEntry]:
    return (await client.activity(EPOCH)).entries


async def test_edit_entry_names_only_the_edited_cell_with_who(tmp_path: Path) -> None:
    async with engine_for(tmp_path, kernel="fake") as engine:
        session, ann, (_a, b, _c) = await notebook(engine, ["x = 1", "y = x", "z = y"])
        bob = session.attach(BOB)
        before = len(await _since_start(ann))
        await bob.apply([ReplaceCell(cell_id=b, source="y = x + 1")], None)
        (entry,) = (await _since_start(ann))[before:]
        assert (entry.kind, entry.change, entry.cell_ids) == ("cell_edit", "edit", [b])
        assert (entry.actor.kind, entry.actor.id, entry.actor.display_name) == (
            "person",
            BOB.id,
            "Bob",
        )
        assert entry.stale_cell_ids == []


async def test_a_mixed_batch_is_one_entry_per_change(tmp_path: Path) -> None:
    async with engine_for(tmp_path, kernel="fake") as engine:
        _, ann, (a, b, c) = await notebook(engine, ["x = 1", "y = x", "z = y"])
        before = len(await _since_start(ann))
        result = await ann.apply(
            [
                InsertCell(source="q = 0", after=a),
                DeleteCell(cell_id=c),
                SetCellName(cell_id=b, name="mid"),
                SetSetting(key="reactivity", value="lazy"),
                MoveCell(cell_id=a, after=a),
            ],
            None,
        )
        entries = (await _since_start(ann))[before:]
        assert _shape(entries) == [
            ("cell_edit", "insert", list(result.created), ANN.id),
            ("cell_edit", "delete", [c], ANN.id),
            ("cell_edit", "rename", [b], ANN.id),
            ("settings_change", None, [], ANN.id),
        ]
        assert entries[-1].settings == ["reactivity"]


async def test_a_batch_that_changes_nothing_adds_nothing(tmp_path: Path) -> None:
    async with engine_for(tmp_path, kernel="fake") as engine:
        _, ann, (a, b) = await notebook(engine, ["x = 1", "y = x"])
        before = len(await _since_start(ann))
        await ann.apply(
            [MoveCell(cell_id=b, after=a), ReplaceCell(cell_id=a, source="x = 1")], None
        )
        assert len(await _since_start(ann)) == before


async def test_a_repeated_submit_is_recorded_once(tmp_path: Path) -> None:
    async with engine_for(tmp_path, kernel="fake") as engine:
        _, ann, (a,) = await notebook(engine, ["x = 1"])
        before = len(await _since_start(ann))
        for _ in range(2):
            await ann.apply([ReplaceCell(cell_id=a, source="x = 2")], None, "submit-0001")
        assert len(await _since_start(ann)) == before + 1


async def test_since_is_strict_and_exclude_actor_drops_one_actors_entries(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    async with engine_for(tmp_path, kernel="fake", clock=clock) as engine:
        session, ann, (a, b) = await notebook(engine, ["x = 1", "y = x"])
        bob = session.attach(BOB)
        clock.advance(10)
        t1 = clock.now()
        await ann.apply([ReplaceCell(cell_id=a, source="x = 2")], None)
        clock.advance(10)
        t2 = clock.now()
        await bob.apply([ReplaceCell(cell_id=b, source="y = x * 2")], None)
        clock.advance(10)
        await ann.apply([ReplaceCell(cell_id=b, source="y = x * 3")], None)

        def who(entries: Sequence[ActivityEntry]) -> list[tuple[str, list[str]]]:
            return [(e.actor.id, e.cell_ids) for e in entries]

        everything = who((await ann.activity(t1 - timedelta(seconds=1))).entries)
        assert everything == [(ANN.id, [a]), (BOB.id, [b]), (ANN.id, [b])]
        # An entry exactly at ``since`` was already seen by whoever passed it.
        assert who((await ann.activity(t1)).entries) == everything[1:]
        assert who((await ann.activity(t2)).entries) == everything[2:]
        others = await ann.activity(t1 - timedelta(seconds=1), exclude_actor=ANN.id)
        assert who(others.entries) == [(BOB.id, [b])]
        assert others.exclude_actor == ANN.id
        assert who((await ann.activity(t1 - timedelta(seconds=1), BOB.id)).entries) == [
            (ANN.id, [a]),
            (ANN.id, [b]),
        ]
        assert (await ann.activity(clock.now())).entries == []


@pytest.mark.parametrize(
    ("reactivity", "stale"),
    [
        pytest.param("lazy", True, id="lazy_leaves_descendants_stale"),
        pytest.param("autorun", False, id="autorun"),
    ],
)
async def test_run_entry_names_its_cells_and_what_it_left_stale(
    tmp_path: Path, reactivity: str, stale: bool
) -> None:
    async with engine_for(tmp_path, kernel="fake") as engine:
        session, ann, (a, b, c) = await notebook(
            engine, ["x = 1", "y = x", "z = 1"], settings={"reactivity": reactivity}
        )
        bob = session.attach(BOB)
        assert (await run_cells(ann, a, b, c)).status == "ok"
        record = await run_cells(bob, a)
        entry = (await _since_start(ann))[-1]
        assert (entry.kind, entry.run_id, entry.status) == ("cell_run", record.run_id, "ok")
        assert entry.actor.display_name == "Bob"
        assert entry.cell_ids == ([a] if stale else [a, b])
        assert entry.stale_cell_ids == ([b] if stale else [])


async def test_run_entry_carries_the_error_class(tmp_path: Path) -> None:
    async with engine_for(tmp_path, kernel="fake") as engine:
        _, ann, (a,) = await notebook(engine, ["x = 1 / 0"])
        await run_cells(ann, a)
        entry = (await _since_start(ann))[-1]
        assert (entry.kind, entry.status, entry.error_class) == (
            "cell_run",
            "error",
            "ZeroDivisionError",
        )


@pytest.mark.parametrize(
    ("action", "kind", "reason"),
    [
        pytest.param("restart", "kernel_restart", "restart", id="restart"),
        pytest.param("shutdown", "kernel_stop", "shutdown", id="shutdown"),
    ],
)
async def test_kernel_entries_name_who_asked_and_why(
    tmp_path: Path, action: Any, kind: str, reason: str
) -> None:
    async with engine_for(tmp_path, kernel="fake") as engine:
        session, ann, (a,) = await notebook(engine, ["x = 1"])
        bob = session.attach(BOB)
        await run_cells(ann, a)
        await bob.kernel(action)
        entries = await until(
            lambda: [e for e in session.runtime.activity_log if e.kind.startswith("kernel")]
        )
        assert [(e.kind, e.reason, e.actor.id) for e in entries] == [(kind, reason, BOB.id)]


async def test_a_kernel_nobody_asked_to_lose_is_the_systems(tmp_path: Path) -> None:
    async with engine_for(tmp_path, kernel="fake") as engine:
        session, ann, (a,) = await notebook(engine, ["x = 1"])
        await run_cells(ann, a)
        kernel = session.runtime.kernel
        assert kernel is not None
        kernel.kill("crashed")
        (entry,) = await until(
            lambda: [e for e in session.runtime.activity_log if e.kind.startswith("kernel")]
        )
        assert (entry.kind, entry.reason) == ("kernel_restart", "crashed")
        assert (entry.actor.kind, entry.actor.id) == ("system", "system")


class _InstallableEnvs(StaticEnvRegistry):
    async def install(
        self, env_id: str, packages: Sequence[str], notebook_path: str
    ) -> tuple[Any, list[str], str]:
        return self.descriptor, [], ""


async def test_settings_and_environment_entries(tmp_path: Path) -> None:
    async with engine_for(tmp_path, kernel="fake", envs=_InstallableEnvs()) as engine:
        session, ann, (a,) = await notebook(engine, ["x = 1"])
        bob = session.attach(BOB)
        await run_cells(ann, a)
        before = len(await _since_start(ann))
        await bob.settings(SettingsChange(sql_row_limit=10))
        await bob.settings(SettingsChange(sql_row_limit=10))
        await bob.settings(SettingsChange(dataframe="pandas"))
        await bob.env(EnvAction(action="switch", env="./other"))
        await bob.env(EnvAction(action="install", packages=["tinypkg"]))
        await bob.env(EnvAction(action="materialize"))
        await bob.env(EnvAction(action="info"))
        entries = await until(
            lambda: found if len(found := session.runtime.activity_log[before:]) >= 6 else None
        )
        assert [(e.kind, e.change, e.reason, e.settings, e.actor.id) for e in entries] == [
            ("settings_change", None, None, ["sql_row_limit"], BOB.id),
            ("settings_change", None, None, ["dataframe"], BOB.id),
            ("env_change", "switch", None, [], BOB.id),
            ("kernel_restart", None, "env_changed", [], BOB.id),
            ("env_change", "install", None, [], BOB.id),
            ("env_change", "materialize", None, [], BOB.id),
        ]


async def test_installing_into_a_script_block_is_one_env_entry(tmp_path: Path) -> None:
    class ScriptEnvs(_InstallableEnvs):
        def __init__(self) -> None:
            super().__init__()
            self.descriptor = dataclasses.replace(self.descriptor, kind="script")

    async with engine_for(tmp_path, kernel="fake", envs=ScriptEnvs()) as engine:
        _, ann, _ = await notebook(engine, ["x = 1"])
        before = len(await _since_start(ann))
        await ann.env(EnvAction(action="install", packages=["tinypkg"]))
        entries = (await _since_start(ann))[before:]
        assert _shape(entries) == [("env_change", "install", [], ANN.id)]
        assert "tinypkg" in (tmp_path / "ws" / "nb.alknb.py").read_text()


# The agent's view ---------------------------------------------------------------------


async def test_agent_port_and_digest_see_who_and_only_the_changed_cell(tmp_path: Path) -> None:
    async with engine_for(tmp_path, kernel="fake") as engine:
        workspace = EngineWorkspace(tmp_path / "ws", engine)
        person = ActorRef(kind="person", id=BOB.id, display_name="Bob")
        agent = ActorRef(kind="agent", id="agent:a", display_name="Agent")
        session, ann, (a, b, c) = await notebook(engine, ["x = 1", "y = x", "z = y"])
        await run_cells(ann, a)
        cursor = (await (await workspace.port("nb.alknb.py", agent)).activity(EPOCH)).cursor
        bob_port = await workspace.port("nb.alknb.py", person)
        from alkera_notebook.tools.models import RenameCellOp, ReplaceCellOp

        await bob_port.apply(
            [ReplaceCellOp(cell_id=b, source="y = x + 1"), RenameCellOp(cell_id=c, name="total")],
            None,
        )
        await bob_port.kernel("restart")
        await until(lambda: any(e.kind == "kernel_restart" for e in session.runtime.activity_log))
        port = await workspace.port("nb.alknb.py", agent)
        activity = await port.activity(cursor)
        assert [(i.kind, i.cell_id, i.actor.display_name) for i in activity.items] == [
            ("edit", b, "Bob"),
            ("rename", c, "Bob"),
            ("kernel", None, "Bob"),
        ]
        assert (activity.items[-1].status, activity.items[-1].reason) == ("restarted", "restart")
        assert activity.cursor > cursor
        part = render_notebook(activity, exclude_actor=agent.id)
        assert part is not None
        assert f"Bob edited {b}" in part.text and f"Bob renamed total ({c})" in part.text
        assert "kernel restarted (restart), by Bob" in part.text
        assert a not in part.text
        assert (await port.activity(activity.cursor)).items == []


async def test_an_interrupt_that_escalates_is_the_interrupters_restart(tmp_path: Path) -> None:
    code = "import signal, time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\ntime.sleep(60)"
    async with engine_for(tmp_path, escalation=(0.3, 1.0)) as engine:
        session, ann, (a,) = await notebook(engine, [code])
        bob = session.attach(BOB)
        handle = await ann.run(CellsTarget(ids=[a]))
        await until(lambda: session.runtime.current is not None and session.runtime.kernel)
        await until(
            lambda: (
                session.runtime.current is not None
                and session.runtime.current.record.status == "running"
            )
        )
        await bob.kernel("interrupt")
        record = await handle.wait(15)
        assert (record.status, record.reason) == ("kernel_restarted", "interrupt_restart")
        entries = await until(
            lambda: [e for e in session.runtime.activity_log if e.kind.startswith("kernel")]
        )
        assert [(e.kind, e.reason, e.actor.id) for e in entries] == [
            ("kernel_restart", "interrupt_restart", BOB.id)
        ]
        run = next(e for e in session.runtime.activity_log if e.kind == "cell_run")
        assert (run.actor.id, run.status) == (ANN.id, "kernel_restarted")
        # The escalation starts a new kernel; let it come up before the engine closes.
        await until(lambda: session.runtime.kernel is not None, timeout_s=15)
