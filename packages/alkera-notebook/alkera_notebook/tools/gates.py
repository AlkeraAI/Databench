"""Which permission each notebook tool call needs, decided before it runs.

A notebook tool call is one of five effects:

- ``read``: never gated (reading a notebook, its outputs, its graph, cached
  variables, a frame page, kernel status, widget values, environment info).
- ``file_edit``: gated as the harness gates an edit of that file (creating or
  editing a notebook, every ``notebook.cells`` action including clearing
  outputs, a setting that does not restart the kernel).
- ``run_cells``: running cells (``notebook.run``, ``notebook.widget set``). The
  subject carries the plan: every cell that would execute, with its code and,
  for a SQL cell, the statement as it will be sent. The harness classifies each
  step with its own classifiers (a Python cell as running it with
  ``python -c``, a SQL cell as ``sql.query`` would on that connection), takes
  the most severe effect, and lets the permission mode decide; a step it cannot
  classify counts as a write.
- ``code_exec``: other calls that run code in the kernel, gated as running
  ``python -c`` through the shell (an interrupt, restart or shutdown,
  summarizing a live value, switching the environment).
- ``env_write``: changes the environment and reaches the network (installing
  packages).

The classification is pure over the validated arguments; the subject a gate is
asked about (:class:`GateSubject`) adds what only the notebook knows: the plan
and code for a run, and whether an interrupt reaches someone else's run. The
harness maps a subject onto its own permission engine; the simulator records
every subject to prove nothing ran ungated.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel

from alkera_notebook.cell_names import cell_display_name
from alkera_notebook.tools.models import (
    NotebookEnvInput,
    NotebookInspectInput,
    NotebookKernelInput,
    NotebookSettingsInput,
    NotebookWidgetInput,
)
from alkera_notebook.tools.port import PlanPreview


class GateEffect(StrEnum):
    READ = "read"
    FILE_EDIT = "file_edit"
    RUN_CELLS = "run_cells"
    CODE_EXEC = "code_exec"
    ENV_WRITE = "env_write"


#: The settings whose change restarts the kernel, and so runs code.
KERNEL_RESTARTING_SETTINGS: frozenset[str] = frozenset({"env"})
#: The ``notebook.env`` actions that only read; every other one is a write.
ENV_READ_ACTIONS: frozenset[str] = frozenset({"info", "list", "packages"})

#: Tools whose every call has one effect regardless of arguments.
_FIXED: dict[str, GateEffect] = {
    "notebook.read": GateEffect.READ,
    "notebook.output": GateEffect.READ,
    "notebook.show_output": GateEffect.READ,
    "notebook.graph": GateEffect.READ,
    "notebook.create": GateEffect.FILE_EDIT,
    "notebook.edit": GateEffect.FILE_EDIT,
    # Every cell action changes what everyone sees, clearing outputs included.
    "notebook.cells": GateEffect.FILE_EDIT,
    "notebook.run": GateEffect.RUN_CELLS,
}


def gate_effect(tool: str, args: BaseModel) -> GateEffect:
    """The effect of calling ``tool`` with ``args``.

    An unknown tool is ``code_exec``: a name this table does not know is
    treated as the strictest gate, never as a read."""
    fixed = _FIXED.get(tool)
    if fixed is not None:
        return fixed
    if tool == "notebook.kernel" and isinstance(args, NotebookKernelInput):
        return GateEffect.READ if args.action == "status" else GateEffect.CODE_EXEC
    if tool == "notebook.inspect" and isinstance(args, NotebookInspectInput):
        return GateEffect.CODE_EXEC if args.what == "value" else GateEffect.READ
    if tool == "notebook.widget" and isinstance(args, NotebookWidgetInput):
        return GateEffect.RUN_CELLS if args.action == "set" else GateEffect.READ
    if tool == "notebook.env" and isinstance(args, NotebookEnvInput):
        # Only what reads is a read: any other action (install, remove,
        # materialize, cancel, and whatever is added later) changes the
        # environment until it is named here.
        if args.action in ENV_READ_ACTIONS:
            return GateEffect.READ
        return GateEffect.CODE_EXEC if args.action == "switch" else GateEffect.ENV_WRITE
    if tool == "notebook.settings" and isinstance(args, NotebookSettingsInput):
        changes = args.changes()
        if KERNEL_RESTARTING_SETTINGS & set(changes):
            return GateEffect.CODE_EXEC
        return GateEffect.FILE_EDIT if changes else GateEffect.READ
    return GateEffect.CODE_EXEC


@dataclass(frozen=True)
class GateSubject:
    """What a gate is asked about."""

    tool: str
    effect: GateEffect
    path: str
    """The notebook's canonical path inside the workspace."""
    lead: str
    """The action's words before the notebook: ``Run 3 cells in``."""
    tail: str = ""
    """The action's words after the notebook: `` (ends work started by Bob)``."""
    detail: str = ""
    """What a person reads before answering: for a run, the plan and every
    step's code; for an install, the packages."""
    affects_others: tuple[str, ...] = ()
    """Names of the other people or agents whose runs this call interrupts or ends."""
    plan: PlanPreview | None = None
    packages: tuple[str, ...] = ()
    """For an install: the packages it installs."""

    @property
    def title(self) -> str:
        """One line naming the action: ``Run 3 cells in analysis.alknb.py``."""
        return f"{self.lead} {self.path}{self.tail}"


@dataclass(frozen=True)
class GateVerdict:
    allowed: bool
    reason: str | None = None


class Gatekeeper(Protocol):
    """Decides a gated call. Implemented by the harness over its permission
    engine, and by the simulator's recording gatekeeper."""

    async def __call__(self, subject: GateSubject) -> GateVerdict: ...


@dataclass
class RecordingGatekeeper:
    """A gatekeeper that answers from a fixed policy per effect and records
    every subject it was asked about, for tests and the simulator."""

    allow: frozenset[GateEffect] = frozenset(GateEffect)
    asked: list[GateSubject] = field(default_factory=list)

    async def __call__(self, subject: GateSubject) -> GateVerdict:
        self.asked.append(subject)
        if subject.effect in self.allow:
            return GateVerdict(True)
        return GateVerdict(False, f"{subject.effect.value} is not allowed here")


#: Why a step runs, as a person reads it beside the cell's name.
_STEP_REASONS: dict[str, str] = {
    "target": "asked for",
    "upstream": "needed by a cell asked for",
    "descendant": "reads a cell asked for",
}


def render_plan(plan: PlanPreview, *, max_chars: int = 12_000) -> str:
    """The plan as a person reads it in a prompt: each step's name (never its
    id), why it runs, and its code, bounded."""
    lines: list[str] = []
    for step in plan.steps:
        name = cell_display_name(step.name, step.index)
        lines.append(f"{name}, {_STEP_REASONS.get(step.reason, step.reason)}:")
        if step.kind == "sql":
            where = step.connection or "local frames"
            note = ", interpolated in the kernel" if step.interpolated else ""
            lines.append(f"-- SQL on {where}{note}")
            lines.append((step.sql or "-- (statement not known before the run)").rstrip())
        else:
            lines.append(step.code.rstrip() or "# (empty)")
        lines.append("")
    if plan.estimate_s is not None:
        lines.append(f"Estimated time from earlier runs: {plan.estimate_s:.0f} s")
    text = "\n".join(lines).rstrip()
    if len(text) > max_chars:
        text = text[: max_chars - 40].rstrip() + "\n(plan shortened for this prompt)"
    return text


def cells_phrase(steps: Sequence[object]) -> str:
    """``1 cell`` / ``3 cells``."""
    count = len(steps)
    return f"{count} {'cell' if count == 1 else 'cells'}"


__all__ = [
    "KERNEL_RESTARTING_SETTINGS",
    "GateEffect",
    "GateSubject",
    "GateVerdict",
    "Gatekeeper",
    "RecordingGatekeeper",
    "cells_phrase",
    "gate_effect",
    "render_plan",
]
