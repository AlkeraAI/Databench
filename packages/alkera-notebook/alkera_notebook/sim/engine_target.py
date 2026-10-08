"""The real engine as a simulation target.

Runs every simulated step against a :class:`~alkera_notebook.engine.NotebookEngine`
on a temporary workspace: the file document store, real kernel subprocesses
through the local launcher, a Unix socket transport and the RSS memory source.

What it exposes to the invariants:

- ``document``: the store's live cells.
- ``file``: the notebook file on disk, read with the format and written back.
- ``runs``: the engine's run records, with when each started and finished,
  and which run produced each cell's current output.
- ``processes``: every kernel process the engine started, checked dead after
  ``close``.

- ``kernel_facts``: per cell, the code it last ran with and whether the kernel
  holds its value (the engine's cell runtime), its last result, and a run
  sequence number derived from the engine's own records: the position of the
  run that produced the cell's current kernel output in the run queue (runs
  execute one at a time in queue order), then the cell's position in that
  run's plan. The engine keeps no per-cell sequence number of its own; this
  derivation is exact as long as runs never interleave, which the run-order
  invariant checks.

Every cell is described by its code (a SQL cell by its ``alkera.sql`` call),
so the graph the invariants rebuild is the format's analysis of exactly what
the kernel ran.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from alkera_notebook.sim.targets import Capability, CellFacts, RunFacts
from alkera_notebook.tools.engine_adapter import EngineWorkspace, local_engine
from alkera_notebook.tools.port import ActorRef, NotebookHost

#: The engine's cell runtime status as the last result of the cell's run.
_RESULT: dict[str, str] = {
    "fresh": "ok",
    "stale": "ok",
    "not_run": "none",
    "error": "error",
    "interrupted": "interrupted",
    "skipped": "skipped",
    "stopped": "stopped",
}


class EngineTarget:
    name = "engine"
    capabilities: frozenset[Capability] = frozenset(
        {"document", "kernel_facts", "runs", "file", "processes"}
    )

    def __init__(self, root: Path, *, budget_bytes: int = 4 * 1024**3, **engine_args: Any) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve()
        self.engine = local_engine(
            self.root,
            data_root=self.root.parent / f"{root.name}-data",
            budget_bytes=budget_bytes,
            **engine_args,
        )
        self.workspace = EngineWorkspace(self.root, self.engine)
        self._pids: set[int] = set()

    def host(self, actor: ActorRef) -> NotebookHost:
        return self.workspace.host(actor)

    def _sessions(self) -> dict[str, Any]:
        return {s.path: s for s in self.engine.sessions}

    def _note_kernels(self) -> None:
        for session in self.engine.sessions:
            kernel = session.kernel
            if kernel is not None:
                self._pids.add(int(kernel.launched.pid))

    async def settle(self, timeout_s: float = 60.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            self._note_kernels()
            busy = [s for s in self.engine.sessions if s.current is not None or len(s.queue) > 0]
            if not busy:
                return
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("runs did not settle")
            await asyncio.sleep(0.05)

    def paths(self) -> list[str]:
        return sorted(self._sessions())

    def document(self, path: str) -> list[tuple[str, str, str, str]]:
        session = self._sessions()[path]
        return [(c.id, c.kind, c.name, c.source) for c in session.doc.live_cells()]

    def kernel_facts(self, path: str) -> dict[str, CellFacts]:
        session = self._sessions()[path]
        order = {run_id: i for i, run_id in enumerate(session.records)}
        out: dict[str, CellFacts] = {}
        for cell in session.doc.live_cells():
            runtime = session.cells.get(cell.id)
            seq = 0
            outputs = session.outputs.get(cell.id)
            meta = getattr(outputs, "run", None)
            if outputs is not None and outputs.origin == "kernel" and meta is not None:
                record = session.records.get(meta.run_id)
                steps = [e.cell_id for e in record.plan] if record is not None else []
                position = steps.index(cell.id) if cell.id in steps else 0
                seq = (order.get(meta.run_id, 0) + 1) * 100_000 + position
            status = runtime.status if runtime is not None else "not_run"
            result = _RESULT.get(status, "ok")
            out[cell.id] = CellFacts(
                kind="python",
                name=cell.name,
                source=cell.code,
                meta={},
                submitted=runtime.submitted if runtime is not None else None,
                result=result,  # type: ignore[arg-type]
                seq=seq,
                has_value=bool(runtime is not None and runtime.holds_value),
                disabled=bool(cell.config.get("disabled")),
            )
        return out

    def reactivity(self, path: str) -> str:
        return str(self._sessions()[path].doc.settings.get("reactivity", "autorun"))

    def reported_statuses(self, path: str) -> dict[str, str]:
        session = self._sessions()[path]
        return {c.id: str(session._cell_status(c.id)) for c in session.doc.live_cells()}

    def reported_graph(self, path: str) -> tuple[list[tuple[str, str]], dict[str, list[str]]]:
        from alkera_notebook.sim.analysis import error_code

        view = self._sessions()[path].graph_view(None, "both")
        errors = {k: [error_code(e) for e in v] for k, v in view.errors.items()}
        return [(a, b) for a, b in view.edges], errors

    def runs(self, path: str) -> list[RunFacts]:
        session = self._sessions()[path]
        return [
            RunFacts(
                run_id=r.run_id,
                requested_by=r.requested_by.id,
                planned=tuple(e.cell_id for e in r.plan),
                status={"queued": "queued", "running": "running"}.get(r.status, "finished"),
                executed=(),
                started_at=r.started_at,
                finished_at=r.finished_at,
            )
            for r in session.records.values()
        ]

    def executions(self, path: str) -> list[tuple[str, str]]:
        """Which run produced each cell's current kernel output."""
        session = self._sessions()[path]
        out: list[tuple[str, str]] = []
        for cid, outputs in session.outputs.items():
            run_id = getattr(outputs, "run_id", None)
            if getattr(outputs, "origin", None) == "kernel" and run_id:
                out.append((str(run_id), cid))
        return out

    def file_check(self, path: str) -> tuple[list[tuple[str, str, str, str]], bool] | None:
        from alkera_notebook import format as nbformat

        text = (self.root / path).read_text(encoding="utf-8")
        ir = nbformat.read(text)
        cells = [(c.id, str(c.kind), c.name, c.source) for c in ir.cells]
        return cells, nbformat.write(ir) == text

    def live_kernel_processes(self) -> Sequence[int]:
        alive = []
        for pid in sorted(self._pids):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                pass
            alive.append(pid)
        return alive

    def memory_samples(self) -> tuple[int, Sequence[int]]:
        return (0, ())

    async def close(self) -> None:
        self._note_kernels()
        await self.workspace.close()


__all__ = ["EngineTarget"]
