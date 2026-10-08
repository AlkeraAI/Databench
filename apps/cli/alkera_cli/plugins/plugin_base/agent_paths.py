"""How a path a tool wrote reads to the agent."""

from __future__ import annotations

from pathlib import Path

from alkera_cli.plugins.plugin_base.tool import ToolContext


def agent_path(ctx: ToolContext, path: Path | str) -> str:
    """A host ``path`` a tool wrote, spelled as the agent sees it.

    A tool runs in the daemon and writes by host path; a bounded chat's agent
    sees its root at the sandbox's own path and the host path not at all, so a
    result that names the host path names a file the model cannot open. The
    session's fence knows the mapping (it judges the agent's spellings by it);
    a session with no fence sees host paths, and gets them.
    """
    spell = getattr(ctx.fence, "spell", None)
    return str(spell(Path(path))) if callable(spell) else str(path)


__all__ = ["agent_path"]
