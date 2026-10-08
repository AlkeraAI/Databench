"""Per-notebook runtime state the engine keeps beside the document.

Plain data: what each cell last ran with in the current kernel, whether it
holds a value, its status, and the jobs in the notebook's run queue. The
session (``engine/session.py``) owns one of each per open notebook.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal

from alkera_notebook.engine.models import (
    Actor,
    RunRecord,
    RunStatus,
    RunTrigger,
    VarSummary,
)
from alkera_notebook.plan.planner import RuntimeStatus, Scope, TargetCode


@dataclass
class CellRuntime:
    status: RuntimeStatus = "not_run"
    # The code the cell last ran with in the current kernel.
    submitted: str | None = None
    holds_value: bool = False
    defs: list[str] = field(default_factory=list)
    duration_s: float | None = None
    activity: Literal["queued", "running"] | None = None
    variables: list[VarSummary] = field(default_factory=list)

    def reset(self) -> None:
        """The kernel went away: nothing ran in the next one yet."""
        self.status = "not_run"
        self.submitted = None
        self.holds_value = False
        self.defs = []
        self.activity = None
        self.variables = []


@dataclass
class Job:
    """One entry of a notebook's run queue (a planned run or a comm delivery)."""

    kind: Literal["run", "comm"]
    run_id: str
    actor: Actor
    trigger: RunTrigger
    record: RunRecord
    done: asyncio.Future[RunRecord]
    scope: Scope = "cells"
    targets: list[str] = field(default_factory=list)
    target_codes: dict[str, str] = field(default_factory=dict)
    target_code: TargetCode = "current"
    confirm: bool = True
    key: tuple[Any, ...] = ()
    # Comm deliveries.
    comm_id: str | None = None
    msg_id: str | None = None
    content: dict[str, Any] = field(default_factory=dict)
    buffers: list[bytes] = field(default_factory=list)
    started: bool = False
    # Widget messages merged into this one while queued (each still gets its
    # idle status when this one is handled).
    merged_msg_ids: list[str] = field(default_factory=list)

    def finish(
        self, status: RunStatus, reason: str | None, now: Any, message: str | None = None
    ) -> RunRecord:
        self.record.status = status
        self.record.reason = reason
        self.record.message = message
        if self.record.finished_at is None:
            self.record.finished_at = now
        if not self.done.done():
            self.done.set_result(self.record)
        return self.record
