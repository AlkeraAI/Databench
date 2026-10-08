"""Every run a session accepted gets a last word, however the session ends:
a run under way or still waiting when the session closes or is suspended is
finished and announced, so nothing that follows runs (a client, the
platform's run rows) is left showing one as queued or running for good."""

from __future__ import annotations

from pathlib import Path

import pytest
from alkera_notebook.engine import CellsTarget, NotebookClient
from alkera_notebook.events.models import RunFinished
from nbeng_harness import engine_for, notebook, until

SLEEP = "import time\ntime.sleep(120)"


def finished(client: NotebookClient) -> dict[str, tuple[str, str | None]]:
    return {
        e.run_id: (e.status, e.reason)
        for e in list(client.queue._items)
        if isinstance(e, RunFinished)
    }


@pytest.mark.parametrize(
    ("ending", "expected"),
    [
        # Closing stops the kernel, and a run waiting on a kernel that exited
        # ends as the kernel's exit.
        pytest.param("close", ("kernel_restarted", "shutdown"), id="closed"),
        pytest.param("suspend", ("interrupted", "suspended"), id="suspended"),
    ],
)
async def test_a_running_and_a_waiting_run_both_end_and_are_announced(
    tmp_path: Path, ending: str, expected: tuple[str, str]
) -> None:
    async with engine_for(tmp_path) as engine:
        session, ann, (a, b) = await notebook(engine, [SLEEP, "1"])
        running = await ann.run(CellsTarget(ids=[a]))
        await until(
            lambda: session.runtime.current is not None and session.runtime.kernel_state == "busy"
        )
        waiting = await ann.run(CellsTarget(ids=[b]))
        assert len(session.runtime.queue) == 1
        if ending == "close":
            await session.runtime.close()
        else:
            await engine.suspend()
        first = await running.wait(30)
        second = await waiting.wait(30)
        assert first.status in ("interrupted", "kernel_restarted")
        assert (second.status, second.reason) == expected
        announced = finished(ann)
        assert announced[waiting.run_id] == expected
        assert announced[running.run_id][0] == first.status


async def test_a_run_asked_while_suspended_is_announced_as_it_ends(tmp_path: Path) -> None:
    """The workspace was put to sleep; a request that still reaches the
    engine is ended at once and its end announced, not left queued."""
    async with engine_for(tmp_path) as engine:
        _session, ann, (a,) = await notebook(engine, ["1"])
        await engine.suspend()
        handle = await ann.run(CellsTarget(ids=[a]))
        record = await handle.wait(30)
        assert (record.status, record.reason) == ("interrupted", "suspended")
        await until(lambda: handle.run_id in finished(ann))
        assert finished(ann)[handle.run_id] == ("interrupted", "suspended")
