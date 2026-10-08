"""A cell that ran fresh reads ``stale`` while a cell above it in the kernel
graph has changed since: edited and not re-run, stale itself, or failed.

The notebook is a chain ``a -> b -> c`` with an unrelated sibling ``s`` and
its own child ``t``, all run fresh before each case."""

from __future__ import annotations

from pathlib import Path

import pytest
from alkera_notebook.document.ops import ReplaceCell
from alkera_notebook.engine import NotebookClient, NotebookEngine, StaleTarget
from alkera_notebook.events.models import CellStatusEvent
from nbeng_harness import BOB, engine_for, notebook, run_cells, statuses

CELLS = ["a = 1", "b = a + 1", "c = b * 2", "s = 10", "t = s + 1"]
NAMES = ("a", "b", "c", "s", "t")


async def _ran(
    engine: NotebookEngine, reactivity: str = "autorun"
) -> tuple[NotebookClient, dict[str, str]]:
    session, client, ids = await notebook(engine, CELLS, settings={"reactivity": reactivity})
    record = await run_cells(client, *ids, timeout_s=60)
    assert record.status == "ok"
    named = dict(zip(NAMES, ids, strict=True))
    assert set((await statuses(client)).values()) == {"fresh"}
    del session
    return client, named


async def _named_statuses(client: NotebookClient, named: dict[str, str]) -> dict[str, str]:
    by_id = await statuses(client)
    return {name: by_id[cid] for name, cid in named.items()}


@pytest.mark.parametrize(
    ("edited", "source", "expected"),
    [
        pytest.param(
            "a",
            "a = 5",
            {"a": "edited", "b": "stale", "c": "stale", "s": "fresh", "t": "fresh"},
            id="the_whole_chain_below_the_root",
        ),
        pytest.param(
            "b",
            "b = a + 2",
            {"a": "fresh", "b": "edited", "c": "stale", "s": "fresh", "t": "fresh"},
            id="only_below_a_middle_cell",
        ),
        pytest.param(
            "c",
            "c = b * 3",
            {"a": "fresh", "b": "fresh", "c": "edited", "s": "fresh", "t": "fresh"},
            id="nothing_below_a_leaf",
        ),
        pytest.param(
            "s",
            "s = 11",
            {"a": "fresh", "b": "fresh", "c": "fresh", "s": "edited", "t": "stale"},
            id="the_sibling_branch_only",
        ),
        pytest.param(
            "a",
            "z = 1",
            {"a": "edited", "b": "stale", "c": "stale", "s": "fresh", "t": "fresh"},
            id="an_edit_that_drops_the_name_still_marks_what_read_it",
        ),
    ],
)
async def test_an_edit_not_re_run_marks_every_descendant_stale(
    tmp_path: Path, edited: str, source: str, expected: dict[str, str]
) -> None:
    async with engine_for(tmp_path) as engine:
        client, named = await _ran(engine)
        await client.apply([ReplaceCell(cell_id=named[edited], source=source)], None)
        assert await _named_statuses(client, named) == expected


async def test_editing_back_to_the_code_that_ran_clears_it(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        client, named = await _ran(engine)
        await client.apply([ReplaceCell(cell_id=named["a"], source="a = 5")], None)
        assert (await _named_statuses(client, named))["c"] == "stale"
        await client.apply([ReplaceCell(cell_id=named["a"], source="a = 1")], None)
        assert set((await _named_statuses(client, named)).values()) == {"fresh"}


@pytest.mark.parametrize(
    ("reactivity", "after_root_run"),
    [
        pytest.param("autorun", "fresh", id="autorun_re_runs_the_descendants"),
        pytest.param("lazy", "stale", id="lazy_leaves_them_stale_until_run"),
    ],
)
async def test_re_running_the_edited_cell(
    tmp_path: Path, reactivity: str, after_root_run: str
) -> None:
    async with engine_for(tmp_path) as engine:
        client, named = await _ran(engine, reactivity)
        await client.apply([ReplaceCell(cell_id=named["a"], source="a = 5")], None)
        assert (await run_cells(client, named["a"], timeout_s=60)).status == "ok"
        got = await _named_statuses(client, named)
        assert got["a"] == "fresh"
        assert (got["b"], got["c"]) == (after_root_run, after_root_run)
        assert (got["s"], got["t"]) == ("fresh", "fresh")


async def test_run_stale_in_lazy_runs_what_reads_stale_below_an_edit(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        client, named = await _ran(engine, "lazy")
        await client.apply([ReplaceCell(cell_id=named["a"], source="a = 5")], None)
        handle = await client.run(StaleTarget())
        record = await handle.wait(60)
        assert record.status == "ok"
        assert [s.cell_id for s in record.plan] == [named["a"], named["b"], named["c"]]
        assert set((await _named_statuses(client, named)).values()) == {"fresh"}


async def test_a_failed_ancestor_leaves_a_lazy_descendant_stale(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        client, named = await _ran(engine, "lazy")
        await client.apply([ReplaceCell(cell_id=named["b"], source="b = a + undefined")], None)
        assert (await run_cells(client, named["b"], timeout_s=60)).status == "error"
        got = await _named_statuses(client, named)
        assert (got["a"], got["b"], got["c"]) == ("fresh", "error", "stale")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("a = 5", "stale", id="an_edit_announces_stale"),
        pytest.param("a = 1", None, id="a_no_op_edit_announces_nothing_below"),
    ],
)
async def test_a_watching_client_is_told_the_descendant_changed(
    tmp_path: Path, source: str, expected: str | None
) -> None:
    async with engine_for(tmp_path) as engine:
        session, client, ids = await notebook(engine, CELLS)
        assert (await run_cells(client, *ids, timeout_s=60)).status == "ok"
        named = dict(zip(NAMES, ids, strict=True))
        watcher = session.attach(BOB)
        while len(watcher.queue):
            await watcher.next_event(1)
        await client.apply([ReplaceCell(cell_id=named["a"], source=source)], None)
        seen: dict[str, str] = {}
        while len(watcher.queue):
            event = await watcher.next_event(1)
            if isinstance(event, CellStatusEvent):
                seen[event.cell_id] = event.status
        assert seen.get(named["c"]) == expected
        assert named["s"] not in seen
