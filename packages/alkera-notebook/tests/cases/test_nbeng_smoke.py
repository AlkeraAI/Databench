"""The engine end to end: a run through a real kernel process."""

from __future__ import annotations

from pathlib import Path

from nbeng_harness import ANN, engine_for, notebook, run_cells, statuses, text_of


async def test_running_a_leaf_runs_its_upstream_and_shows_output(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _, client, (a, b) = await notebook(engine, ["x = 20", "y = x + 1\ny"])
        record = await run_cells(client, b)
        assert record.status == "ok", record
        assert [(p.cell_id, p.reason) for p in record.plan] == [(a, "upstream"), (b, "target")]
        assert record.requested_by == ANN
        assert await text_of(client, b) == "21"
        assert await statuses(client) == {a: "fresh", b: "fresh"}
