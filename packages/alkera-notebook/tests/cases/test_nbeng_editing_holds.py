"""A cell someone is editing: when an autorun skips it, and what happens next.

Real kernels over the file store. A caret is published the way a client
publishes it (``store.focus``); the clock is moved by hand, so "left the
cell", "still there" and "vanished without clearing" are told apart by what
was said, not by how long the test slept.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alkera_notebook.document.ops import ReplaceCell, SetSetting
from alkera_notebook.engine import AllTarget
from nbeng_fakes import ManualClock
from nbeng_harness import ANN, BOB, engine_for, notebook, run_cells, statuses, text_of, until

PATH = "nb.alknb.py"
SOURCES = ["x = 1", "y = x + 1\ny"]


async def _waits_for(client: Any, cid: str) -> str | None:
    return {c.id: c.rerun_waits_for for c in (await client.read()).cells}[cid]


async def _skips(client: Any, cid: str) -> list[str]:
    return [
        n.message
        for n in (await client.read()).notices
        if n.kind == "cell_skipped_editing" and n.cell_id == cid
    ]


async def _edit_and_run_a(ann: Any, a: str, value: int) -> list[str]:
    await ann.apply([ReplaceCell(cell_id=a, source=f"x = {value}")], None)
    return [p.cell_id for p in (await run_cells(ann, a)).plan]


async def test_a_cell_someone_left_reruns_with_the_next_autorun(tmp_path: Path) -> None:
    """Bob typed in b and clicked out one second ago. Ann's run re-runs b at
    once: no hold, no notice."""
    clock = ManualClock()
    async with engine_for(tmp_path, clock=clock) as engine:
        _, ann, (a, b) = await notebook(engine, SOURCES)
        await (await ann.run(AllTarget())).wait(30)
        await engine.store.focus(PATH, BOB, b)
        await engine.store.apply(
            PATH, [ReplaceCell(cell_id=b, source="y = x + 1\ny ")], None, BOB, None
        )
        await engine.store.focus(PATH, BOB, None)
        clock.advance(1)
        assert await _edit_and_run_a(ann, a, 5) == [a, b]
        assert await text_of(ann, b) == "6"
        assert await _waits_for(ann, b) is None
        assert await _skips(ann, b) == []


async def test_a_cell_someone_is_in_waits_and_reruns_when_they_leave(tmp_path: Path) -> None:
    clock = ManualClock()
    async with engine_for(tmp_path, clock=clock) as engine:
        _, ann, (a, b) = await notebook(engine, SOURCES)
        await (await ann.run(AllTarget())).wait(30)
        await engine.store.focus(PATH, BOB, b)
        assert await _edit_and_run_a(ann, a, 5) == [a]
        assert await text_of(ann, b) == "2"
        assert (await statuses(ann))[b] == "stale"
        assert await _waits_for(ann, b) == "Bob"
        assert await _skips(ann, b) == ["Not re-run while Bob is editing it"]
        # Bob stays, confirming his caret as a client does: b keeps waiting
        # well past any window.
        for _ in range(6):
            clock.advance(10)
            await engine.store.focus(PATH, BOB, b)
        assert await _waits_for(ann, b) == "Bob"
        assert await text_of(ann, b) == "2"
        await engine.store.focus(PATH, BOB, None)
        await until(lambda: _text_is(ann, b, "6"))
        assert await _waits_for(ann, b) is None
        assert (await statuses(ann))[b] == "fresh"


async def _text_is(client: Any, cid: str, want: str) -> bool:
    return await text_of(client, cid) == want


async def test_a_vanished_peer_stops_holding_the_cell_after_the_fallback(tmp_path: Path) -> None:
    clock = ManualClock()
    async with engine_for(tmp_path, clock=clock) as engine:
        _, ann, (a, b) = await notebook(engine, SOURCES)
        await (await ann.run(AllTarget())).wait(30)
        await engine.store.focus(PATH, BOB, b)  # and Bob's tab dies
        assert await _edit_and_run_a(ann, a, 5) == [a]
        clock.advance(30)
        assert await _waits_for(ann, b) == "Bob"
        assert await text_of(ann, b) == "2"
        clock.advance(1)
        await until(lambda: _text_is(ann, b, "6"))
        assert await _waits_for(ann, b) is None


async def test_the_requester_s_own_caret_holds_the_cell_until_it_moves(tmp_path: Path) -> None:
    """Ann is mid-edit in b and runs a: b's half-typed text never runs, b
    waits for her, and re-runs with the code the kernel knows once her caret
    leaves."""
    clock = ManualClock()
    async with engine_for(tmp_path, clock=clock) as engine:
        _, ann, (a, b) = await notebook(engine, SOURCES)
        await (await ann.run(AllTarget())).wait(30)
        await engine.store.focus(PATH, ANN, b)
        assert await _edit_and_run_a(ann, a, 5) == [a]
        assert await _waits_for(ann, b) == "Ann"
        assert await _skips(ann, b) == ["Not re-run while Ann is editing it"]
        await engine.store.focus(PATH, ANN, a)
        await until(lambda: _text_is(ann, b, "6"))
        assert (await ann.output(b, "error")).error is None


async def test_the_requester_s_caret_in_another_cell_holds_nothing(tmp_path: Path) -> None:
    """Ann edited b seconds ago, and her caret is in a now: her own earlier
    presence in b does not stop her run from re-running it."""
    clock = ManualClock()
    async with engine_for(tmp_path, clock=clock) as engine:
        _, ann, (a, b) = await notebook(engine, SOURCES)
        await (await ann.run(AllTarget())).wait(30)
        await engine.store.focus(PATH, ANN, b)
        await engine.store.focus(PATH, ANN, a)
        assert await _edit_and_run_a(ann, a, 5) == [a, b]
        assert await _waits_for(ann, b) is None


async def test_a_held_cell_whose_text_changes_stops_waiting_and_is_not_rerun(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    async with engine_for(tmp_path, clock=clock) as engine:
        session, ann, (a, b) = await notebook(engine, SOURCES)
        bob = session.attach(BOB)
        await (await ann.run(AllTarget())).wait(30)
        await engine.store.focus(PATH, BOB, b)
        assert await _edit_and_run_a(ann, a, 5) == [a]
        assert await _waits_for(ann, b) == "Bob"
        await bob.apply([ReplaceCell(cell_id=b, source="y = x + 100\ny")], None)
        await until(lambda: _is_none(ann, b))
        await engine.store.focus(PATH, BOB, None)
        clock.advance(5)
        for _ in range(20):
            await clock.sleep(0.01)
        assert await text_of(ann, b) == "2"
        assert (await statuses(ann))[b] == "edited"


async def _is_none(client: Any, cid: str) -> bool:
    return await _waits_for(client, cid) is None


async def test_a_held_cell_stays_stale_once_the_notebook_is_lazy(tmp_path: Path) -> None:
    clock = ManualClock()
    async with engine_for(tmp_path, clock=clock) as engine:
        _, ann, (a, b) = await notebook(engine, SOURCES)
        await (await ann.run(AllTarget())).wait(30)
        await engine.store.focus(PATH, BOB, b)
        assert await _edit_and_run_a(ann, a, 5) == [a]
        await ann.apply([SetSetting(key="reactivity", value="lazy")], None)
        await engine.store.focus(PATH, BOB, None)
        await until(lambda: _is_none(ann, b))
        for _ in range(20):
            await clock.sleep(0.01)
        assert await text_of(ann, b) == "2"
        assert (await statuses(ann))[b] == "stale"


async def test_a_kernel_restart_ends_every_hold(tmp_path: Path) -> None:
    clock = ManualClock()
    async with engine_for(tmp_path, clock=clock) as engine:
        _, ann, (a, b) = await notebook(engine, SOURCES)
        await (await ann.run(AllTarget())).wait(30)
        await engine.store.focus(PATH, BOB, b)
        assert await _edit_and_run_a(ann, a, 5) == [a]
        assert await _waits_for(ann, b) == "Bob"
        await ann.kernel("restart")
        assert await _waits_for(ann, b) is None
        await engine.store.focus(PATH, BOB, None)
        for _ in range(20):
            await clock.sleep(0.01)
        assert (await statuses(ann))[b] == "not_run"
