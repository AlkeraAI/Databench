"""The per-client event queue: coalescing, overflow and closing."""

from __future__ import annotations

import pytest
from alkera_notebook.engine import KernelInfo, NotebookView, Settings
from alkera_notebook.events.models import (
    CellOutputEvent,
    CellStatusEvent,
    CellStreamEvent,
    Resync,
)
from alkera_notebook.events.queue import ClientQueue, EventHub


async def view() -> NotebookView:
    return NotebookView(
        path="nb.alknb.py",
        token="r1",
        settings=Settings(),
        kernel=KernelInfo(state="absent", env=None, reactivity="autorun"),
        cells=[],
    )


def out(cell: str, text: str, mode: str = "replace", run: str = "r") -> CellOutputEvent:
    return CellOutputEvent(cell_id=cell, run_id=run, output={"text/plain": text}, mode=mode)  # type: ignore[arg-type]


async def drain(q: ClientQueue) -> list[object]:
    items = []
    while len(q):
        items.append(await q.get())
    return items


async def test_status_for_one_cell_keeps_only_the_latest_moved_to_the_end() -> None:
    q = ClientQueue(10, view)
    q.put(CellStatusEvent(cell_id="a", status="queued"))
    q.put(CellStatusEvent(cell_id="b", status="queued"))
    q.put(CellStatusEvent(cell_id="a", status="running"))
    got = await drain(q)
    assert [(e.cell_id, e.status) for e in got] == [("b", "queued"), ("a", "running")]  # type: ignore[attr-defined]


def _st(cell: str, status: str) -> CellStatusEvent:
    return CellStatusEvent(cell_id=cell, status=status, run_id="r")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("puts", "kept"),
    [
        pytest.param(
            [_st("a", "queued"), _st("a", "running")],
            [("a", "running")],
            id="queued-replaced-by-running",
        ),
        pytest.param(
            [_st("a", "running"), _st("a", "fresh")],
            [("a", "running"), ("a", "fresh")],
            id="running-is-never-replaced",
        ),
        pytest.param(
            [_st("a", "queued"), out("a", "1", "append"), _st("a", "fresh")],
            [("a", "queued"), ("a", "out"), ("a", "fresh")],
            id="never-across-the-cells-own-output",
        ),
        pytest.param(
            [_st("a", "queued"), out("b", "1", "append"), _st("a", "fresh")],
            [("b", "out"), ("a", "fresh")],
            id="across-another-cells-output",
        ),
    ],
)
async def test_a_status_replaces_only_a_status_the_client_may_skip(
    puts: list[object], kept: list[tuple[str, str]]
) -> None:
    q = ClientQueue(10, view)
    for event in puts:
        q.put(event)  # type: ignore[arg-type]
    got = [
        (e.cell_id, e.status if isinstance(e, CellStatusEvent) else "out")  # type: ignore[attr-defined]
        for e in await drain(q)
    ]
    assert got == kept


async def test_replace_output_replaces_a_pending_replace_in_place() -> None:
    q = ClientQueue(10, view)
    q.put(out("a", "1"))
    q.put(CellStreamEvent(cell_id="a", run_id="r", name="stdout", text="x"))
    q.put(out("a", "2"))
    got = await drain(q)
    assert [type(e).__name__ for e in got] == ["CellOutputEvent", "CellStreamEvent"]
    assert got[0].output == {"text/plain": "2"}  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("first_mode", "second_run"),
    [
        pytest.param("append", "r", id="after_an_append"),
        pytest.param("replace", "other", id="from_another_run"),
    ],
)
async def test_replace_never_jumps_over_an_append_or_another_run(
    first_mode: str, second_run: str
) -> None:
    q = ClientQueue(10, view)
    q.put(out("a", "1", mode=first_mode))
    q.put(out("a", "2", run=second_run))
    got = await drain(q)
    assert [e.output["text/plain"] for e in got] == ["1", "2"]  # type: ignore[attr-defined]


async def test_overflow_drops_pending_and_yields_one_resync_with_the_view() -> None:
    hub = EventHub()
    q = hub.subscribe(3, view)
    for i in range(10):
        hub.publish(out(f"c{i}", str(i)))
    assert q.overflows == 1 and len(q) == 0
    first = await q.get()
    assert isinstance(first, Resync) and first.view is not None and first.seq == 10
    hub.publish(out("z", "after"))
    assert (await q.get()).cell_id == "z"  # type: ignore[union-attr]


async def test_closed_queue_drains_then_ends_and_ignores_new_events() -> None:
    hub = EventHub()
    q = hub.subscribe(5, view)
    hub.publish(out("a", "1"))
    hub.unsubscribe(q)
    hub.publish(out("b", "2"))
    assert q.closed
    assert [e.cell_id async for e in q] == ["a"]  # type: ignore[attr-defined]


def test_queue_needs_room_for_a_resync() -> None:
    with pytest.raises(ValueError):
        ClientQueue(1, view)


async def test_listeners_see_every_event_with_increasing_seq() -> None:
    hub = EventHub()
    seen: list[int] = []
    hub.listen(lambda e: seen.append(e.seq))
    for i in range(3):
        hub.publish(out("a", str(i)))
    assert seen == [1, 2, 3]
    hub.close()
    assert hub.queues == []
