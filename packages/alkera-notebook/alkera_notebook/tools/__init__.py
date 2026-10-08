"""The agent tools for notebooks: shapes, gates, functions and guidance.

Install with the ``alkera-notebook[agent]`` extra. A harness serves these tools
by calling :func:`call_tool` with a :class:`NotebookHost` for its workspace and
a :class:`Gatekeeper` over its own permission engine.
"""

from __future__ import annotations

from alkera_notebook.tools.catalog import TOOLS, ToolDef, call_tool, gate_subject, validate
from alkera_notebook.tools.gates import (
    GateEffect,
    Gatekeeper,
    GateSubject,
    GateVerdict,
    RecordingGatekeeper,
    gate_effect,
)
from alkera_notebook.tools.guidance import (
    NOTEBOOK_GUIDE,
    PROMPT_BLOCK,
    SKILL_BODY,
    SKILL_DESCRIPTION,
    SKILL_NAME,
    GuideLedger,
)
from alkera_notebook.tools.paging import ResultSpill
from alkera_notebook.tools.port import (
    ActorRef,
    NotebookHost,
    NotebookPort,
    NotebookToolError,
)

__all__ = [
    "NOTEBOOK_GUIDE",
    "PROMPT_BLOCK",
    "SKILL_BODY",
    "SKILL_DESCRIPTION",
    "SKILL_NAME",
    "TOOLS",
    "ActorRef",
    "GateEffect",
    "GateSubject",
    "GateVerdict",
    "Gatekeeper",
    "GuideLedger",
    "NotebookHost",
    "NotebookPort",
    "NotebookToolError",
    "RecordingGatekeeper",
    "ResultSpill",
    "ToolDef",
    "call_tool",
    "gate_effect",
    "gate_subject",
    "validate",
]
