"""The notebook run sweep activity: a thin wrapper around the sweep in
``worker.tasks.notebooks``, under its advisory lock so two sweeps never race
for the same rows."""

from __future__ import annotations

from temporalio import activity

from worker.tasks._hardening import run_locked
from worker.tasks.notebooks import sweep_notebook_runs


@activity.defn(name="notebooks.sweep_runs")
async def sweep_runs() -> int:
    """Every minute: end the notebook runs no engine will end. Returns how
    many ended; zero when another sweep held the lock."""
    ended = await run_locked("notebooks_sweep_runs", sweep_notebook_runs)
    return ended or 0
