"""The subagent result envelope.

Lives in its own module so BOTH ``tool.py`` (which types ``ToolContext.spawn``)
and ``subagent_tool.py`` / the harness ``runtime`` can share it without an import
cycle. This module imports nothing from the rest of ``plugin_base``.

``AgentUsageStats`` is the tool/usage counts carried in the spawn result.
``SubagentRunResult`` is the in-process value the runtime hands back to the spawn
tool. It carries ``error`` so a child failure routes up as a clean tool-call
error instead of a parent-crashing exception.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel


class AgentUsageStats(BaseModel):
    """The tool/usage counts a spawn result carries back to the parent."""

    tool_calls: int = 0
    """Total tool calls the child made."""
    by_tool: dict[str, int] = {}
    """Per-tool-name counts, e.g. ``{"read": 18, "grep": 4, "fetch_result": 2}``."""
    files_read: int = 0
    """Distinct file paths the child read (best-effort from read/grep/glob inputs)."""
    duration_seconds: float = 0.0
    model: str | None = None
    """The gateway model slug the child actually ran on (``None`` = inherited)."""
    truncated: bool = False
    """True iff the exploration phase hit the time budget and was force-summarized.
    It is NOT an error."""


@dataclass(frozen=True, slots=True)
class SubagentRunResult:
    """The runtime's in-process return from ``ChatSession.spawn_subagent``.

    ``error`` is ``None`` on success; when set, the spawn tool raises ``ToolError``
    so the dispatcher surfaces ``{error, tool}``. The child's failure
    never propagates as a parent-crashing exception, so concurrent siblings + the
    parent turn survive.
    """

    summary: str
    stats: AgentUsageStats
    child_session_id: str = ""
    """The spawned child chat's session id. It rides on the tool result so the
    parent-child link lives in the tool call itself, and the spawn renders as one
    tool card."""
    error: str | None = None


__all__ = ["AgentUsageStats", "SubagentRunResult"]
