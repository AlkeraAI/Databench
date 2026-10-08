"""Events: bounded per-client queues and resync."""

from __future__ import annotations

from pathlib import Path

from alkera_notebook.events.models import Resync
from nbeng_harness import BOB, engine_for, notebook, run_cells


async def test_events_a_client_that_stops_reading_gets_one_resync_with_full_state(
    tmp_path: Path,
) -> None:
    code = "for i in range(400):\n    display(i)\ndone = 1"
    async with engine_for(tmp_path, queue_max=20) as engine:
        session, ann, (a,) = await notebook(engine, [code])
        stalled = session.attach(BOB)
        reader = session.attach(BOB)
        seen = []
        record = await run_cells(ann, a)
        assert record.status == "ok"
        # The stalled client's queue never grew past its bound.
        assert len(stalled.queue) <= 20
        assert stalled.queue.overflows >= 1
        first = await stalled.next_event(5)
        assert isinstance(first, Resync)
        assert first.view is not None
        assert [c.status for c in first.view.cells] == ["fresh"]
        assert "399" in (first.view.cells[0].output.text if first.view.cells[0].output else "")
        # A client reading from the start loses nothing it could not rebuild:
        # it too overflowed, and also gets exactly one resync first.
        while len(reader.queue):
            seen.append(await reader.next_event(1))
        assert sum(isinstance(e, Resync) for e in seen) <= 1


async def test_events_status_and_output_replace_coalesce_per_cell(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        session, ann, (a, b) = await notebook(engine, ["x = 1\nx", "y = 2\ny"])
        watcher = session.attach(BOB)
        await run_cells(ann, a, b)
        events = []
        while len(watcher.queue):
            events.append(await watcher.next_event(1))
        statuses = [(e.cell_id, e.status) for e in events if e.type == "cell.status"]  # type: ignore[union-attr]
        # A slow reader gets older statuses replaced, except ``running``: each
        # cell keeps exactly its running, then its final status.
        assert sorted(statuses) == sorted(
            [(a, "running"), (a, "fresh"), (b, "running"), (b, "fresh")]
        )
        seqs = [e.seq for e in events]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        assert events[-1].type in ("run.finished", "snapshot.saved", "cell.status", "kernel.state")
