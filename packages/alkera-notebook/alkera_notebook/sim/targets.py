"""What the simulator drives, and what it may look at to check invariants.

A :class:`SimTarget` gives every simulated actor a
:class:`~alkera_notebook.tools.port.NotebookHost` and exposes the engine's
inner facts the invariants compare against what the tools reported. Each
target declares its :attr:`~SimTarget.capabilities`; an invariant that needs
a fact the target cannot give is reported as not checked, never as passed.

Capabilities:

- ``document``: the store's live cells (id, kind, name, source).
- ``kernel_facts``: per cell, the code it last ran with, its last result, its
  run sequence number and whether the kernel holds its value.
- ``runs``: every run with its plan, status, and the cells it executed in order.
- ``file``: the notebook file on disk and the format reader and writer.
- ``processes``: the kernel processes the engine started.
- ``memory``: the memory samples the guard saw and its budget.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from alkera_notebook.format import file_code
from alkera_notebook.sim.reference import ReferenceWorkspace
from alkera_notebook.tools.port import ActorRef, NotebookHost

Capability = Literal["document", "kernel_facts", "runs", "file", "processes", "memory"]


@dataclass(frozen=True)
class CellFacts:
    kind: str
    name: str
    source: str
    meta: dict[str, Any]
    submitted: str | None
    result: Literal["none", "ok", "error", "interrupted", "skipped", "stopped"]
    seq: int
    has_value: bool
    disabled: bool


@dataclass(frozen=True)
class RunFacts:
    run_id: str
    requested_by: str
    planned: tuple[str, ...]
    status: str
    executed: tuple[str, ...]
    started_at: datetime | None = None
    finished_at: datetime | None = None


class SimTarget(Protocol):
    name: str
    capabilities: frozenset[Capability]

    def host(self, actor: ActorRef) -> NotebookHost: ...

    async def settle(self, timeout_s: float = 30.0) -> None:
        """Wait until no run is queued or running (bounded)."""
        ...

    def paths(self) -> list[str]: ...

    def document(self, path: str) -> list[tuple[str, str, str, str]]:
        """The live cells in order: (id, kind, name, source)."""
        ...

    def kernel_facts(self, path: str) -> dict[str, CellFacts]: ...

    def reactivity(self, path: str) -> str: ...

    def reported_statuses(self, path: str) -> dict[str, str]:
        """The statuses a read reports now."""
        ...

    def reported_graph(self, path: str) -> tuple[list[tuple[str, str]], dict[str, list[str]]]:
        """The graph the engine plans by: its edges and its errors per cell."""
        ...

    def runs(self, path: str) -> list[RunFacts]: ...

    def executions(self, path: str) -> list[tuple[str, str]]:
        """(run id, cell id) of every executed step, in execution order."""
        ...

    def file_check(self, path: str) -> tuple[list[tuple[str, str, str, str]], bool] | None:
        """The cells the file reads to and whether writing them back gives the
        same bytes; ``None`` without the ``file`` capability."""
        ...

    def live_kernel_processes(self) -> Sequence[int]: ...

    def memory_samples(self) -> tuple[int, Sequence[int]]:
        """The budget and the samples over it, in sampling order."""
        ...

    async def close(self) -> None: ...


class ReferenceTarget:
    """The in-process reference engine as a simulation target."""

    name = "reference"
    capabilities: frozenset[Capability] = frozenset({"document", "kernel_facts", "runs"})

    def __init__(
        self, workspace: ReferenceWorkspace | None = None, *, check_file: bool = False
    ) -> None:
        self.workspace = workspace or ReferenceWorkspace()
        if check_file:
            # The reference engine keeps no file of its own: with this on, every
            # check writes its document with the format and reads it back, which
            # puts the format's round trip under the simulator's random documents.
            self.capabilities = self.capabilities | {"file"}

    def host(self, actor: ActorRef) -> NotebookHost:
        return self.workspace.host(actor)

    async def settle(self, timeout_s: float = 30.0) -> None:
        import asyncio

        for notebook in self.workspace.notebooks.values():
            worker = notebook.worker
            if worker is not None and not worker.done():
                await asyncio.wait_for(asyncio.shield(worker), timeout=timeout_s)

    def paths(self) -> list[str]:
        return sorted(self.workspace.notebooks)

    def document(self, path: str) -> list[tuple[str, str, str, str]]:
        return self.workspace.notebooks[path].doc.signature()

    def kernel_facts(self, path: str) -> dict[str, CellFacts]:
        nb = self.workspace.notebooks[path]
        out: dict[str, CellFacts] = {}
        for cell in nb.doc.live():
            state = nb.kstate.get(cell.id)
            out[cell.id] = CellFacts(
                kind=cell.kind,
                name=cell.name,
                source=cell.source,
                meta=dict(cell.meta),
                submitted=state.submitted if state else None,
                result=state.result if state else "none",
                seq=state.seq if state else 0,
                has_value=state.has_value if state else False,
                disabled=bool(cell.config.get("disabled")),
            )
        return out

    def reactivity(self, path: str) -> str:
        return str(self.workspace.notebooks[path].doc.settings.get("reactivity", "autorun"))

    def reported_statuses(self, path: str) -> dict[str, str]:
        return dict(self.workspace.notebooks[path].statuses())

    def reported_graph(self, path: str) -> tuple[list[tuple[str, str]], dict[str, list[str]]]:
        graph = self.workspace.notebooks[path].kernel_graph()
        return list(graph.edges), {k: list(v) for k, v in graph.errors.items()}

    def runs(self, path: str) -> list[RunFacts]:
        nb = self.workspace.notebooks[path]
        return [
            RunFacts(
                run_id=r.run_id,
                requested_by=r.requested_by.id,
                planned=tuple(s.cell_id for s in r.steps),
                status=r.status,
                executed=tuple(r.executed),
            )
            for r in nb.runs
        ]

    def executions(self, path: str) -> list[tuple[str, str]]:
        return list(self.workspace.notebooks[path].executions)

    def file_text(self, path: str) -> str:
        """The notebook as the format writes it."""
        from alkera_notebook import format as nbformat
        from alkera_notebook.sim.analysis import cell_code

        nb = self.workspace.notebooks[path]
        cells = []
        for c in nb.doc.live():
            # The format keeps no trailing whitespace; a store writes text it can hold.
            text = file_code(c.source)
            cells.append(
                nbformat.CellIR(
                    id=c.id,
                    kind=c.kind,  # type: ignore[arg-type]
                    name=c.name,
                    source=text,
                    code=cell_code(c.kind, text, c.meta),
                    config=dict(c.config),
                    meta=dict(c.meta),
                )
            )
        settings = {k: v for k, v in nb.doc.settings.items() if k != "format"}
        return nbformat.write(nbformat.NotebookIR(settings=settings, cells=cells))

    def file_check(self, path: str) -> tuple[list[tuple[str, str, str, str]], bool] | None:
        from alkera_notebook import format as nbformat

        text = self.file_text(path)
        ir = nbformat.read(text)
        # The file names a setup cell `setup` whatever the document calls it.
        doc = {cid: (kind, name) for cid, kind, name, _ in self.document(path)}
        cells = [
            (
                c.id,
                str(c.kind),
                doc[c.id][1] if c.id in doc and doc[c.id][0] == "setup" else c.name,
                c.source,
            )
            for c in ir.cells
        ]
        return cells, nbformat.write(ir) == text

    def live_kernel_processes(self) -> Sequence[int]:
        return ()

    def memory_samples(self) -> tuple[int, Sequence[int]]:
        return (0, ())

    async def close(self) -> None:
        await self.workspace.close()


__all__ = ["Capability", "CellFacts", "ReferenceTarget", "RunFacts", "SimTarget"]
