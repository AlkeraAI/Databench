"""A generated huge notebook in the reference engine, for the tools' scaling tests.

Five thousand cells as a file that big would load: 50 chains of 100 cells,
ten cells of 2,000 lines, a SQL cell holding a 1,000,000-row frame, large
text outputs, a long traceback, thousands of kernel variables and widgets,
and an environment with thousands of packages. The state is set the way a
loaded file and a kernel that already ran would leave it, without running
5,000 cells first.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from typing import Any

from alkera_notebook.sim.oracle import Cell
from alkera_notebook.sim.reference import CellKernelState, ReferenceWorkspace, Widget
from alkera_notebook.tools import ActorRef, GateEffect, RecordingGatekeeper, call_tool, validate
from alkera_notebook.tools.port import ErrorRecord, OutputRecord
from pydantic import BaseModel

AGENT = ActorRef(kind="agent", id="agent:t", display_name="Agent")
PATH = "huge.alknb.py"
CELLS = 5_000
CHAIN = 100
HUGE_LINES = 2_000
HUGE_EVERY = 500
#: A token that appears in exactly one line of one cell's source.
NEEDLE = "zebra_quantile_needle"
NEEDLE_CELL = "c2345"
NEEDLE_LINE = 3
TEXT_CELL = "c42"
TEXT_CHARS = 1_500_000
ERROR_CELL = "c77"
FRAME_CELL = "big"
FRAME_ROWS = 1_000_000
IMAGE_CELL = "c88"
VARIABLES = 3_000
WIDGETS = 2_000
PACKAGES = 3_000


def cell_name(i: int) -> str:
    return f"c{i}"


def huge_source(i: int) -> str:
    lines = [f"v{i}_{j} = {j}  # line {j} of a long cell" for j in range(HUGE_LINES - 1)]
    return "\n".join([*lines, f"x{i} = x{i - 1} + v{i}_0"])


def source(i: int) -> str:
    if i % HUGE_EVERY == 7:
        return huge_source(i)
    head = f"x{i} = {i}" if i % CHAIN == 0 else f"x{i} = x{i - 1} + 1"
    body = [f"# step {i}: an ordinary cell", "_tmp = [n * n for n in range(10)]", head]
    if cell_name(i) == NEEDLE_CELL:
        body.insert(NEEDLE_LINE, f"_note = '{NEEDLE}'")
    return "\n".join(body)


@dataclass
class Huge:
    ws: ReferenceWorkspace
    gate: RecordingGatekeeper
    ids: list[str] = field(default_factory=list)
    by_name: dict[str, str] = field(default_factory=dict)
    text: str = ""

    @property
    def nb(self) -> Any:
        return self.ws.notebooks[PATH]

    async def call(self, tool: str, **args: Any) -> BaseModel:
        args.setdefault("path", PATH)
        return await call_tool(
            tool, validate(tool, args), host=self.ws.host(AGENT), gatekeeper=self.gate
        )


@functools.cache
def big_frame() -> Any:
    """The frame, built once per process (tests only read it)."""
    import pandas as pd

    return pd.DataFrame({"row": range(FRAME_ROWS), "label": [f"r{n}" for n in range(FRAME_ROWS)]})


async def build() -> Huge:
    ws = ReferenceWorkspace()
    gate = RecordingGatekeeper(allow=frozenset(GateEffect))
    huge = Huge(ws, gate)
    await huge.call("notebook.create", cells=[])
    nb = huge.nb
    doc = nb.doc.copy()
    doc.order.clear()
    doc.cells.clear()

    def add(name: str, src: str, kind: str = "python", meta: dict[str, Any] | None = None) -> str:
        cid = f"{len(doc.order):010d}".translate(str.maketrans("01", "ab"))
        doc.cells[cid] = Cell(id=cid, kind=kind, name=name, source=src, meta=meta or {})
        doc.order.append(cid)
        huge.ids.append(cid)
        huge.by_name[name] = cid
        return cid

    add("setup", "import alkera", kind="setup")
    for i in range(1, CELLS - 1):
        add(cell_name(i), source(i))
    add(FRAME_CELL, "SELECT * FROM range(1000000)", kind="sql", meta={"output_var": FRAME_CELL})
    nb.doc = doc

    nb.kernel_state = "idle"
    nb._reset_kernel_namespace()
    nb.namespace[FRAME_CELL] = big_frame()
    huge.text = "".join(f"output line {n:07d}\n" for n in range(TEXT_CHARS // 20))

    def ran(name: str, output: OutputRecord | None, bound: list[str] | None = None) -> None:
        cid = huge.by_name[name]
        nb.kstate[cid] = CellKernelState(
            submitted=doc.cells[cid].source,
            has_value=True,
            result="ok",
            seq=1,
            output=output,
            bound=bound or [],
        )

    ran(TEXT_CELL, OutputRecord(kinds=["text/plain"], text=huge.text, author="Agent"))
    nb.kstate[huge.by_name[ERROR_CELL]] = CellKernelState(
        submitted=doc.cells[huge.by_name[ERROR_CELL]].source,
        result="error",
        output=OutputRecord(
            kinds=["application/vnd.alkera.error+json"],
            text="partial output\n" * 5_000,
            error=ErrorRecord(
                ename="ValueError",
                evalue="bad value " * 10_000,
                traceback="".join(f'  File "<cell>", line {n}\n' for n in range(20_000)),
            ),
            author="Agent",
        ),
    )
    ran(
        FRAME_CELL,
        OutputRecord(
            kinds=["application/vnd.alkera.table+json", "text/plain"],
            text="row label\n" * 2_000,
            has_table=True,
            author="Agent",
        ),
    )
    ran(IMAGE_CELL, OutputRecord(kinds=["image/png"], has_image=True, author="Agent"))
    nb.kstate[huge.by_name[IMAGE_CELL]].images = [
        b"\x89PNG" + bytes(n * 1024) for n in (1, 100, 400)
    ]
    for i in range(100, 3_000, 7):
        ran(
            cell_name(i),
            OutputRecord(kinds=["text/plain"], text=f"value {i}\n" * 400, author="Agent"),
        )

    names = [f"var{n:05d}" for n in range(VARIABLES)]
    for n, name in enumerate(names):
        nb.namespace[name] = list(range(n % 50 * 20))
    nb.kstate[huge.by_name["c5"]] = CellKernelState(
        submitted=doc.cells[huge.by_name["c5"]].source, has_value=True, result="ok", bound=names
    )
    for n in range(WIDGETS):
        model_id = f"w-{n:05d}"
        nb.widgets[model_id] = Widget(
            model_id, huge.by_name["c6"], "slider", list(range(500)), [f"widget{n}"]
        )
    nb.installed = {f"pkg{n:05d}" for n in range(PACKAGES)}
    return huge


__all__ = [
    "AGENT",
    "CELLS",
    "CHAIN",
    "ERROR_CELL",
    "FRAME_CELL",
    "FRAME_ROWS",
    "HUGE_EVERY",
    "HUGE_LINES",
    "IMAGE_CELL",
    "NEEDLE",
    "NEEDLE_CELL",
    "NEEDLE_LINE",
    "PACKAGES",
    "PATH",
    "TEXT_CELL",
    "VARIABLES",
    "WIDGETS",
    "Huge",
    "build",
    "cell_name",
    "huge_source",
]
