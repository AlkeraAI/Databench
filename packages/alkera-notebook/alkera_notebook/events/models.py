"""Events the engine sends to each attached client.

Discriminated by ``type``. Every event carries ``seq`` (monotonic per
session) and, where a kernel is involved, ``kernel_id``. A client that falls
behind its bounded queue receives one :class:`Resync` carrying a full view
instead of the events it missed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from alkera_notebook.document.ops import CellNotice, GraphSummary
from alkera_notebook.engine.models import (
    Actor,
    CellStatus,
    EnvInfo,
    ErrorInfo,
    KernelState,
    NotebookView,
    PlanEntry,
    RunStatus,
    RunTrigger,
    VarSummary,
)


class _Event(BaseModel):
    seq: int = 0
    kernel_id: str | None = None


class KernelStateEvent(_Event):
    type: Literal["kernel.state"] = "kernel.state"
    state: KernelState
    # The environment the kernel runs in, on a state of a kernel (starting
    # or running); ``None`` when no kernel is meant.
    env_id: str | None = None
    memory_bytes: int | None = None
    threshold_bytes: int | None = None


class KernelExited(_Event):
    type: Literal["kernel.exited"] = "kernel.exited"
    # "shutdown", "suspended", "crashed", "out_of_memory", "interrupt_restart",
    # "restart", "connection_lost", "start_failed", "idle".
    reason: str
    exit_code: int | None = None
    peak_rss_bytes: int | None = None
    limit_bytes: int | None = None
    largest_process: int | None = None
    message: str | None = None


class KernelInterrupt(_Event):
    """One step of an interrupt's escalation for the run it targets: the
    first signal, the second one when the run has not ended, then the
    kernel's restart when that did not take either; ``done`` when the run
    ended, however it ended (its ``run.finished`` follows)."""

    type: Literal["kernel.interrupt"] = "kernel.interrupt"
    run_id: str
    step: Literal["signalled", "second_signal", "restarting", "done"]
    at: datetime


class RunQueued(_Event):
    type: Literal["run.queued"] = "run.queued"
    run_id: str
    requested_by: Actor
    trigger: RunTrigger
    position: int


class RunStarted(_Event):
    type: Literal["run.started"] = "run.started"
    run_id: str
    plan: list[PlanEntry]


class RunNeedsConfirmation(_Event):
    type: Literal["run.needs_confirmation"] = "run.needs_confirmation"
    run_id: str
    plan: list[PlanEntry]
    estimate_s: float
    reasons: list[str] = Field(default_factory=list)


class CellStatusEvent(_Event):
    type: Literal["cell.status"] = "cell.status"
    cell_id: str
    status: CellStatus
    run_id: str | None = None
    # Whose editing the cell's re-run waits for; None when it waits for nobody.
    rerun_waits_for: str | None = None


class CellOutputEvent(_Event):
    type: Literal["cell.output"] = "cell.output"
    cell_id: str
    run_id: str | None
    output: dict[str, Any]
    mode: Literal["replace", "append"] = "replace"


class CellOutputsCleared(_Event):
    """Someone cleared these cells' outputs: every view drops them, and the
    saved outputs no longer hold them. The cells' values stay in the kernel."""

    type: Literal["cell.outputs_cleared"] = "cell.outputs_cleared"
    cell_ids: list[str]
    actor_id: str | None = None


class CellStreamEvent(_Event):
    type: Literal["cell.stream"] = "cell.stream"
    cell_id: str
    run_id: str | None
    name: Literal["stdout", "stderr"]
    text: str


class CellFinishedEvent(_Event):
    type: Literal["cell.finished"] = "cell.finished"
    cell_id: str
    run_id: str
    status: CellStatus
    error: ErrorInfo | None = None
    duration_ms: int | None = None


class CellVariablesEvent(_Event):
    type: Literal["cell.variables"] = "cell.variables"
    cell_id: str
    run_id: str | None = None
    variables: list[VarSummary]


class RunFinished(_Event):
    type: Literal["run.finished"] = "run.finished"
    run_id: str
    status: RunStatus
    reason: str | None = None
    # Why, in words a reader is shown (``reason`` is the code).
    message: str | None = None


class GraphEvent(_Event):
    type: Literal["graph"] = "graph"
    graph: GraphSummary


class FrameMessage(_Event):
    """A widget message for one output frame, in the frame contract's shape
    (``comm.open`` replays and opens, ``comm.msg``, ``comm.close``,
    ``comm.status``). Sent only to the client that owns the frame."""

    type: Literal["frame.message"] = "frame.message"
    frame_id: str
    message: dict[str, Any]
    buffers: list[bytes] = Field(default_factory=list)


class FrameAttached(BaseModel):
    """An output frame attached at the widget hub: its id (the client's own,
    or one the engine chose), what it shows, and the comm-open replays of
    its models' closure in creation order. Later messages for it arrive as
    ``frame.message`` events carrying ``frame_id``."""

    frame_id: str
    output_id: str | None = None
    model_ids: list[str]
    opens: list[FrameMessage] = Field(default_factory=list)


class DocChanged(_Event):
    type: Literal["doc.changed"] = "doc.changed"
    token: str
    actor_id: str | None
    cell_ids: list[str]
    origin: Literal["ops", "external", "deleted"] = "ops"


class NoticeEvent(_Event):
    type: Literal["notice"] = "notice"
    notice: CellNotice


class SnapshotSaved(_Event):
    type: Literal["snapshot.saved"] = "snapshot.saved"
    path: str
    cells: int


class EnvStateEvent(_Event):
    type: Literal["env.state"] = "env.state"
    env: EnvInfo


class Resync(_Event):
    type: Literal["resync"] = "resync"
    reason: Literal["overflow", "reattach"] = "overflow"
    view: NotebookView | None = None


NotebookEvent = Annotated[
    KernelStateEvent
    | KernelExited
    | KernelInterrupt
    | RunQueued
    | RunStarted
    | RunNeedsConfirmation
    | CellStatusEvent
    | CellOutputEvent
    | CellOutputsCleared
    | CellStreamEvent
    | CellFinishedEvent
    | CellVariablesEvent
    | RunFinished
    | GraphEvent
    | FrameMessage
    | DocChanged
    | NoticeEvent
    | SnapshotSaved
    | EnvStateEvent
    | Resync,
    Field(discriminator="type"),
]

AnyEvent = (
    KernelStateEvent
    | KernelExited
    | KernelInterrupt
    | RunQueued
    | RunStarted
    | RunNeedsConfirmation
    | CellStatusEvent
    | CellOutputEvent
    | CellOutputsCleared
    | CellStreamEvent
    | CellFinishedEvent
    | CellVariablesEvent
    | RunFinished
    | GraphEvent
    | FrameMessage
    | DocChanged
    | NoticeEvent
    | SnapshotSaved
    | EnvStateEvent
    | Resync
)
