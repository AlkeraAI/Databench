"""Shapes that cross Temporal workflow history.

History outlives the process that wrote it — a workflow started before a
deploy replays its inputs and activity results on the new code — so every
shape here is a ``VersionedModel`` with fixtures and a lineage test, never a
bare dataclass or ``BaseModel``.
"""

from alkera_core.schemas.temporal.drain import DrainInput, DrainOutcome, DrainReport
from alkera_core.schemas.temporal.sweep import SweepInput
from alkera_core.schemas.temporal.tool_call import ToolCallActivityInput

__all__ = ["DrainInput", "DrainOutcome", "DrainReport", "SweepInput", "ToolCallActivityInput"]
