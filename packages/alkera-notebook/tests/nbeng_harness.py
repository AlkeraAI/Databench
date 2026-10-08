"""A real engine for the case catalogue: the file store on ``tmp_path``, the
local kernel launcher, a Unix socket transport, the RSS memory source
with a configurable budget, a fake SQL provider and sequential ids."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from alkera_notebook.document.file_store import FileDocumentStore
from alkera_notebook.document.fmt import default_format
from alkera_notebook.document.ops import InsertCell
from alkera_notebook.engine import (
    Actor,
    CellsTarget,
    EngineConfig,
    MemoryPolicy,
    NotebookClient,
    NotebookEngine,
    NotebookSession,
    OutputLimits,
    RunRecord,
    SequentialIds,
    SystemClock,
)
from alkera_notebook.envs.static import StaticEnvRegistry
from alkera_notebook.kernels.launcher import LocalSubprocessLauncher, UnixSocketTransport
from alkera_notebook.memory.source import ProcessRssSource
from alkera_notebook.sql.provider import SqlProviderRegistry
from nbeng_fakes import FAKE_MOUNT

GiB = 1024**3
MiB = 1024**2

ANN = Actor(kind="person", id="u-ann", display_name="Ann", can_edit=True, can_run=True)
BOB = Actor(kind="person", id="u-bob", display_name="Bob", can_edit=True, can_run=True)
AGENT = Actor(kind="agent", id="a-agent", display_name="Agent", can_edit=True, can_run=True)
VIEWER = Actor(kind="person", id="u-view", display_name="Vic", can_edit=False, can_run=False)


def _mount(kernel: str | None) -> str | None:
    """The real kernel runtime by default (assembled under the engine's data
    root); the stand-in kernel for cases that need its test helpers, or when
    ``NBENG_KERNEL=fake``."""
    choice = kernel or os.environ.get("NBENG_KERNEL", "real")
    return FAKE_MOUNT if choice == "fake" else None


@asynccontextmanager
async def engine_for(
    tmp_path: Path,
    *,
    budget: int = 8 * GiB,
    memory: MemoryPolicy | None = None,
    envs: Any = None,
    sql: Any = None,
    metered: set[str] | None = None,
    escalation: tuple[float, float] = (0.5, 1.5),
    max_kernels: int = 4,
    clock: Any = None,
    kernel: str | None = None,
    mount: str | None = None,
    launcher: Any = None,
    cost_guard_seconds: float = 60.0,
    name: str = "ws",
    queue_max: int = 1000,
    idle_s: float | None = None,
    output_frame_url: str | None = None,
    widget_assets: Any = None,
    store_wrapper: Callable[[FileDocumentStore], Any] | None = None,
    shared_envs: bool = True,
) -> AsyncIterator[NotebookEngine]:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    clock = clock or SystemClock()
    fmt = default_format()
    files = FileDocumentStore(str(root), fmt=fmt, clock=clock)
    store = store_wrapper(files) if store_wrapper is not None else files
    config = EngineConfig(
        workspace_root=str(root),
        env_root=str(tmp_path / f"{name}-envs"),
        data_root=str(tmp_path / f"{name}-data"),
        kernel_mount=mount or _mount(kernel),
        interrupt_escalation_s=escalation,
        max_kernels=max_kernels,
        memory=memory or MemoryPolicy(reserve_bytes=0),
        limits=OutputLimits(snapshot_debounce_s=0.05),
        cost_guard_seconds=cost_guard_seconds,
        client_queue_max=queue_max,
        metered_connections=sorted(metered or ()),
        kernel_idle_seconds=idle_s,
        output_frame_url=output_frame_url,
        shared_envs=shared_envs,
    )
    engine = NotebookEngine(
        config,
        store=store,
        launcher=launcher or LocalSubprocessLauncher(),
        transport=UnixSocketTransport(),
        memory=ProcessRssSource(budget),
        sql=SqlProviderRegistry([sql] if sql is not None else []),
        envs=envs or StaticEnvRegistry(),
        clock=clock,
        ids=SequentialIds(),
        fmt=fmt,
        widget_assets=widget_assets,
    )
    try:
        yield engine
    finally:
        await engine.close()


async def notebook(
    engine: NotebookEngine,
    cells: Sequence[str | InsertCell],
    *,
    path: str = "nb.alknb.py",
    actor: Actor = ANN,
    settings: dict[str, Any] | None = None,
) -> tuple[NotebookSession, NotebookClient, list[str]]:
    inserts = [c if isinstance(c, InsertCell) else InsertCell(source=c) for c in cells]
    session = await engine.create(path, inserts, settings or {}, actor)
    client = session.attach(actor)
    view = await client.read()
    return session, client, [c.id for c in view.cells]


async def run_cells(
    client: NotebookClient, *ids: str, timeout_s: float = 30, **kw: Any
) -> RunRecord:
    handle = await client.run(CellsTarget(ids=list(ids)), **kw)
    return await handle.wait(timeout_s)


async def until(
    predicate: Callable[[], Any], timeout_s: float = 10.0, interval: float = 0.02
) -> Any:
    deadline = time.monotonic() + timeout_s
    while True:
        value = predicate()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        if time.monotonic() > deadline:
            raise TimeoutError("condition not reached")
        await asyncio.sleep(interval)


async def statuses(client: NotebookClient) -> dict[str, str]:
    view = await client.read()
    return {c.id: c.status for c in view.cells}


async def text_of(client: NotebookClient, cid: str) -> str:
    return (await client.output(cid, "text")).text.strip()


def alloc_code(var: str, mb: int, step_mb: int = 25, pause: float = 0.05) -> str:
    """Cell code that allocates ``mb`` MiB in ``step_mb`` steps, touching every
    page so the memory is resident, pausing between steps. The blocks sit
    behind an object with a short ``repr`` so the kernel's variable summary
    stays cheap."""
    return (
        "import time\n"
        "class _Held:\n"
        "    def __init__(self):\n"
        "        self.blocks = []\n"
        "    def __repr__(self):\n"
        "        return f'<{len(self.blocks)} blocks>'\n"
        f"{var} = _Held()\n"
        f"for _step in range({mb // step_mb}):\n"
        f"    _block = bytearray({step_mb} * 1048576)\n"
        "    _block[::4096] = b'\\x01' * len(range(0, len(_block), 4096))\n"
        f"    {var}.blocks.append(_block)\n"
        f"    time.sleep({pause})\n"
    )
