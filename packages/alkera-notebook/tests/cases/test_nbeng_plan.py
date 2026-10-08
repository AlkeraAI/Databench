"""Planning through a real engine: which cells a run executes, with which code.

Every case runs real kernels. "Someone is editing" is produced the way it
happens for an actor that publishes no caret: it applies an edit through the
store, which counts as it being in that cell for 15 s. Carets are covered in
``test_nbeng_editing_holds.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alkera_notebook.document.ops import EditCell, InsertCell, ReplaceCell, TextEdit
from alkera_notebook.engine import (
    AboveTarget,
    AllTarget,
    BelowTarget,
    CellsTarget,
    StaleTarget,
)
from nbeng_harness import (
    AGENT,
    BOB,
    engine_for,
    notebook,
    run_cells,
    statuses,
    text_of,
)


def reasons(record: object) -> list[tuple[str, str]]:
    return [(p.cell_id, p.reason) for p in record.plan]  # type: ignore[attr-defined]


async def test_plan_not_run_upstream_runs_first(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _, ann, (a, b, c) = await notebook(engine, ["x = 1", "y = x + 1", "z = y * 10\nz"])
        record = await run_cells(ann, c)
        assert reasons(record) == [(a, "upstream"), (b, "upstream"), (c, "target")]
        assert await text_of(ann, c) == "20"


async def test_plan_fresh_upstream_is_not_rerun(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _, ann, (_a, b) = await notebook(engine, ["import random\nx = random.random()", "y = x\ny"])
        await run_cells(ann, b)
        first = await text_of(ann, b)
        record = await run_cells(ann, b)
        assert reasons(record) == [(b, "target")]
        assert await text_of(ann, b) == first


async def test_plan_edited_ancestor_holding_a_value_is_not_rerun(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _, ann, (a, b) = await notebook(engine, ["x = 1", "y = x + 1\ny"])
        await run_cells(ann, b)
        await ann.apply([ReplaceCell(cell_id=a, source="x = 100")], None)
        record = await run_cells(ann, b)
        assert reasons(record) == [(b, "target")]
        assert await text_of(ann, b) == "2"
        assert (await statuses(ann))[a] == "edited"


async def test_plan_autorun_reruns_descendants_with_submitted_code(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _, ann, (a, b, c) = await notebook(engine, ["x = 1", "y = x + 1", "z = y * 10\nz"])
        await ann.run(AllTarget())
        await (await ann.run(AllTarget())).wait(30)
        # Ann changes c's text without running it; then runs a.
        await ann.apply([ReplaceCell(cell_id=c, source="z = y * 1000\nz")], None)
        await ann.apply([ReplaceCell(cell_id=a, source="x = 2")], None)
        record = await run_cells(ann, a)
        assert reasons(record) == [(a, "target"), (b, "descendant"), (c, "descendant")]
        assert await text_of(ann, c) == "30"  # submitted code, not the live * 1000
        assert (await statuses(ann))[c] == "edited"


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param("not_run", id="plan.autorun_leaves_not_run"),
        pytest.param("error", id="plan.autorun_leaves_error"),
        pytest.param("skipped", id="plan.autorun_leaves_skipped"),
    ],
)
async def test_plan_autorun_leaves_descendants_without_values_alone(
    tmp_path: Path, broken: str
) -> None:
    async with engine_for(tmp_path) as engine:
        sources = ["x = 1", "y = x + 1", "import os\nw = x + 3\nw"]
        if broken == "error":
            sources[1] = "y = x + undefined_name"
        if broken == "skipped":
            sources = ["x = 1", "q = x + undefined_name", "y = q + 1"]
        _, ann, (a, b, c) = await notebook(engine, sources)
        if broken == "not_run":
            await run_cells(ann, c)  # a and c run, b never does
        else:
            await (await ann.run(AllTarget())).wait(30)
        before = await statuses(ann)
        if broken == "skipped":
            assert before[b] == "error" and before[c] == "skipped"
        else:
            assert before[b] == broken
        record = await run_cells(ann, a)
        ran = [cid for cid, _ in reasons(record)]
        if broken == "skipped":
            assert ran == [a]
            assert (await statuses(ann))[c] == "skipped"
        else:
            assert b not in ran
            assert (await statuses(ann))[b] == broken


async def test_plan_autorun_skips_a_descendant_someone_is_editing(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        session, ann, (a, b) = await notebook(engine, ["x = 1", "y = x + 1\ny"])
        bob = session.attach(BOB)
        await (await ann.run(AllTarget())).wait(30)
        # Bob is mid-way through rewriting b; his live text would raise.
        await bob.apply([ReplaceCell(cell_id=b, source="y = LIVE_TEXT_NEVER_RUNS(x)\ny")], None)
        await ann.apply([ReplaceCell(cell_id=a, source="x = 5")], None)
        record = await run_cells(ann, a)
        assert reasons(record) == [(a, "target")]
        assert await text_of(ann, b) == "2"
        view = await ann.read()
        skipped = [n for n in view.notices if n.cell_id == b][-1]
        assert skipped.kind == "cell_skipped_editing"
        assert skipped.message == "Not re-run while Bob is editing it"
        assert skipped.by == "Bob"
        assert (await ann.output(b, "error")).error is None


async def test_plan_agent_unfinished_multi_op_edit_is_not_executed(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        session, ann, (a, b, c) = await notebook(engine, ["x = 1", "y = x + 1\ny", "z = y + 1\nz"])
        agent = session.attach(AGENT)
        await (await ann.run(AllTarget())).wait(30)
        # The agent's first op of a two-op rewrite: a and c now read names
        # that its second op would have defined.
        await agent.apply(
            [
                EditCell(cell_id=a, edits=[TextEdit(old="x = 1", new="x = half_done()")]),
                EditCell(cell_id=c, edits=[TextEdit(old="y + 1", new="y + not_yet()")]),
            ],
            None,
        )
        record = await run_cells(ann, b)
        assert reasons(record) == [(b, "target")]  # a holds a value; c is being edited
        assert record.status == "ok"
        assert await text_of(ann, c) == "3"


async def test_plan_upstream_being_edited_fails_the_run(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        session, ann, (a, b) = await notebook(
            engine, [InsertCell(source="x = 1", name="load"), "y = x\ny"]
        )
        bob = session.attach(BOB)
        await bob.apply([ReplaceCell(cell_id=a, source="x = 2")], None)
        handle = await ann.run(CellsTarget(ids=[b]))
        assert handle.info.status == "refused"
        assert handle.info.reason == "upstream_being_edited"
        record = await handle.wait(5)
        assert record.status == "refused"
        notices = [n for n in (await ann.read()).notices if n.kind == "upstream_being_edited"]
        assert notices[-1].message == "cell `load` is being edited by Bob"
        assert (await ann.kernel("status")).state == "absent"


async def test_plan_after_resume_running_a_leaf_runs_only_its_ancestors(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _, ann, (a, b, c, d) = await notebook(
            engine, ["x = 1", "y = x + 1", "z = y + 1\nz", "other = 7"]
        )
        await (await ann.run(AllTarget())).wait(30)
        await engine.suspend()
        await engine.resume()
        assert set((await statuses(ann)).values()) == {"not_run"}
        record = await run_cells(ann, c)
        assert reasons(record) == [(a, "upstream"), (b, "upstream"), (c, "target")]
        assert (await statuses(ann))[d] == "not_run"


async def test_plan_lazy_marks_descendants_stale(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _, ann, (a, b) = await notebook(
            engine, ["x = 1", "y = x + 1\ny"], settings={"reactivity": "lazy"}
        )
        await (await ann.run(AllTarget())).wait(30)
        await ann.apply([ReplaceCell(cell_id=a, source="x = 9")], None)
        record = await run_cells(ann, a)
        assert reasons(record) == [(a, "target")]
        assert await statuses(ann) == {a: "fresh", b: "stale"}
        assert await text_of(ann, b) == "2"
        stale = await (await ann.run(StaleTarget())).wait(30)
        assert [cid for cid, _ in reasons(stale)] == [b]
        assert await text_of(ann, b) == "10"


async def test_plan_diamond_runs_each_cell_once(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _, ann, (a, b, c, d) = await notebook(
            engine,
            [
                "import itertools\ncount = itertools.count()\nx = 1",
                "y = x + next(count)",
                "z = x + next(count)",
                "w = (y, z, next(count))\nw",
            ],
        )
        record = await run_cells(ann, d)
        ids = [cid for cid, _ in reasons(record)]
        assert sorted(ids) == sorted([a, b, c, d]) and len(ids) == 4
        assert await text_of(ann, d) == "(1, 2, 2)"


async def test_plan_disabled_cells_never_run(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _, ann, (a, b, c) = await notebook(
            engine,
            ["x = 1", InsertCell(source="y = x + 1", config={"disabled": True}), "z = y\nz"],
        )
        record = await (await ann.run(AllTarget())).wait(30)
        assert [cid for cid, _ in reasons(record)] == [a]
        st = await statuses(ann)
        assert st[b] == "disabled" and st[c] == "not_run"


async def test_plan_upstream_error_skips_dependents(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _, ann, (a, b, _c) = await notebook(engine, ["x = 1 / 0", "y = x + 1", "independent = 3"])
        record = await (await ann.run(AllTarget())).wait(30)
        assert record.status == "error"
        st = await statuses(ann)
        assert st[a] == "error" and st[b] == "skipped"
        err = (await ann.output(a, "error")).error
        assert err is not None and err.ename == "ZeroDivisionError"


async def test_plan_stop_ends_the_cell_and_skips_dependents(tmp_path: Path) -> None:
    # The stand-in kernel's ``stop`` until the public ``alkera.stop`` lands.
    async with engine_for(tmp_path, kernel="fake") as engine:
        _, ann, (a, b) = await notebook(engine, ["x = 1\nstop(x == 1)", "y = x + 1"])
        record = await (await ann.run(AllTarget())).wait(30)
        st = await statuses(ann)
        assert st[a] == "stopped" and st[b] == "skipped"
        assert record.status == "ok"


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        pytest.param("all", [0, 1, 2, 3], id="plan.run_all"),
        pytest.param("above", [0, 1], id="plan.run_above"),
        pytest.param("below", [2, 3], id="plan.run_below"),
        pytest.param("stale", [1, 3], id="plan.run_stale"),
    ],
)
async def test_plan_scopes(tmp_path: Path, target: str, expected: list[int]) -> None:
    async with engine_for(tmp_path) as engine:
        _, ann, ids = await notebook(engine, ["a = 1", "b = 2", "c = 3", "d = 4"])
        if target == "stale":
            await run_cells(ann, ids[0], ids[2])
        if target == "all":
            t = AllTarget()
        elif target == "above":
            t = AboveTarget(id=ids[2])
        elif target == "below":
            t = BelowTarget(id=ids[2])
        else:
            t = StaleTarget()
        record = await (await ann.run(t)).wait(30)
        assert [cid for cid, _ in reasons(record)] == [ids[i] for i in expected]
        assert {r for _, r in reasons(record)} == {"target"}


async def test_plan_cost_guard_needs_confirmation(tmp_path: Path) -> None:
    async with engine_for(tmp_path, cost_guard_seconds=0.3) as engine:
        _, ann, (a, b) = await notebook(engine, ["import time\ntime.sleep(0.4)\nx = 1", "y = x"])
        await (await ann.run(AllTarget())).wait(30)
        await ann.kernel("restart")
        # a ran for 0.4 s; it is an implicit upstream step of b now.
        handle = await ann.run(CellsTarget(ids=[b]))
        assert handle.info.status == "needs_confirmation"
        assert handle.info.estimate_s is not None and handle.info.estimate_s >= 0.4
        assert [p.cell_id for p in handle.info.plan] == [a, b]
        assert (await statuses(ann))[a] == "not_run"
        confirmed = await (await ann.run(CellsTarget(ids=[b]), confirm_expensive=True)).wait(30)
        assert confirmed.status == "ok"
        assert [cid for cid, _ in reasons(confirmed)] == [a, b]


async def test_plan_cost_guard_metered_sql_upstream(tmp_path: Path) -> None:
    async with engine_for(tmp_path, metered={"Warehouse"}) as engine:
        sql_cell = InsertCell(
            kind="sql", source="SELECT 1", meta={"output_var": "df", "connection": "Warehouse"}
        )
        _, ann, (_a, b) = await notebook(engine, [sql_cell, "n = df"])
        handle = await ann.run(CellsTarget(ids=[b]))
        assert handle.info.status == "needs_confirmation"
        assert handle.info.reason == "metered_sql"


async def test_plan_only_returns_code_and_sql_without_queueing(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        sql_cell = InsertCell(
            kind="sql",
            source="SELECT * FROM t WHERE n > {x}",
            meta={"output_var": "df", "connection": "Warehouse"},
        )
        _, ann, (a, b, c) = await notebook(engine, ["x = 1", sql_cell, "n = len(df)"])
        handle = await ann.run(CellsTarget(ids=[c]), plan_only=True)
        assert handle.info.status == "planned"
        entries = [(p.cell_id, p.reason, p.kind) for p in handle.info.plan]
        assert entries == [
            (a, "upstream", "python"),
            (b, "upstream", "sql"),
            (c, "target", "python"),
        ]
        sql_entry = handle.info.plan[1]
        assert sql_entry.sql == "SELECT * FROM t WHERE n > {x}"
        assert sql_entry.connection == "Warehouse" and sql_entry.interpolated
        assert handle.info.plan[0].code == "x = 1"
        assert (await ann.kernel("status")).state == "absent"
        assert (await ann.kernel("status")).queue == []
