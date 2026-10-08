"""The background job-management tools (``background_status`` / ``background_cancel``).

Drives them through ``ToolRegistry.dispatch`` (the path both backends use) against a
real ``BackgroundJobRegistry`` passed as ``ctx.background`` — pinning the status
views (relative ``started_ago`` via ``format_age_ago``, state, an agent job's
child id), the cancel transition, and the root-only gating (hidden from a child's
descriptors AND refused at dispatch when no spawn is wired).
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable
from pathlib import Path

from alkera_cli.harness.background import BackgroundJobRegistry
from alkera_cli.plugins.plugin_base import ToolRegistry
from alkera_cli.plugins.plugin_base.agent_result import AgentUsageStats, SubagentRunResult
from alkera_cli.plugins.plugin_base.background_tools import register_background_tools
from alkera_cli.plugins.plugin_base.mcp_entry import alkera_tool_descriptors
from alkera_cli.plugins.plugin_base.meta_tools import register_meta_tools
from alkera_core.project.directory import ProjectDirectory


def _registry(tmp_path: Path) -> ToolRegistry:
    registry = ToolRegistry(ProjectDirectory(tmp_path / ".alkera").blobs())
    register_meta_tools(registry)
    register_background_tools(registry)
    return registry


def _ids() -> Callable[[], str]:
    counter = itertools.count(1)
    return lambda: f"job_{next(counter):03d}"


async def _spawn_present(prompt: str, **_kw: object) -> SubagentRunResult:
    """A non-None spawn binding (marks a ROOT session so the backstop allows the
    background tools)."""
    return SubagentRunResult(summary="", stats=AgentUsageStats())


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


# --------------------------------------------------------------------------- #
# background_status
# --------------------------------------------------------------------------- #


async def test_status_lists_running_and_completed_with_native_fields(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    bg = BackgroundJobRegistry(id_factory=_ids())
    gate = asyncio.Event()

    async def _running() -> None:
        await gate.wait()

    async def _agent() -> SubagentRunResult:
        return SubagentRunResult(
            summary="found it", stats=AgentUsageStats(), child_session_id="child-xyz"
        )

    bg.submit(_running, kind="bash", title="dev server")
    bg.submit(_agent, kind="agent", title="bug hunt")
    await _settle()  # let the agent job complete

    out = await registry.dispatch("background_status", {}, spawn=_spawn_present, background=bg)
    jobs = {j["job_id"]: j for j in out["jobs"]}
    assert set(jobs) == {"job_001", "job_002"}

    running = jobs["job_001"]
    assert running["kind"] == "bash"
    assert running["title"] == "dev server"
    assert running["state"] == "running"
    assert running["started_ago"]  # a relative "time ago" string, never a raw stamp
    assert running["completed_ago"] is None

    done = jobs["job_002"]
    assert done["kind"] == "agent"
    assert done["state"] == "completed"
    assert done["completed_ago"]  # relative
    assert done["child_session_id"] == "child-xyz"  # the agent job's native child link
    assert done["error"] is None

    gate.set()
    await _settle()


async def test_status_by_job_id_returns_one(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    bg = BackgroundJobRegistry(id_factory=_ids())

    async def _work() -> str:
        return "ok"

    bg.submit(_work, kind="sql", title="q")
    await _settle()

    out = await registry.dispatch(
        "background_status", {"job_id": "job_001"}, spawn=_spawn_present, background=bg
    )
    assert len(out["jobs"]) == 1
    assert out["jobs"][0]["job_id"] == "job_001"
    assert out["jobs"][0]["state"] == "completed"


async def test_status_unknown_job_id_errors(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    bg = BackgroundJobRegistry(id_factory=_ids())
    out = await registry.dispatch(
        "background_status", {"job_id": "nope"}, spawn=_spawn_present, background=bg
    )
    assert "no background job" in out["error"]


# --------------------------------------------------------------------------- #
# background_cancel
# --------------------------------------------------------------------------- #


async def test_cancel_transitions_running_to_cancelled(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    bg = BackgroundJobRegistry(id_factory=_ids())
    started = asyncio.Event()

    async def _daemon() -> None:
        started.set()
        await asyncio.Event().wait()

    bg.submit(_daemon, kind="bash", title="server")
    await started.wait()

    out = await registry.dispatch(
        "background_cancel", {"job_id": "job_001"}, spawn=_spawn_present, background=bg
    )
    assert out["job_id"] == "job_001"
    assert out["state"] == "cancelled"
    assert out["cancelled"] is True
    assert not bg.has_running()


async def test_cancel_unknown_job_errors(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    bg = BackgroundJobRegistry(id_factory=_ids())
    out = await registry.dispatch(
        "background_cancel", {"job_id": "nope"}, spawn=_spawn_present, background=bg
    )
    assert "no background job" in out["error"]


async def test_cancel_of_already_finished_job_reports_not_cancelled(tmp_path: Path) -> None:
    # `cancelled` means "this call transitioned a RUNNING job" — a no-op cancel of an
    # already-completed job must report cancelled=False (its true contract).
    registry = _registry(tmp_path)
    bg = BackgroundJobRegistry(id_factory=_ids())

    async def _work() -> str:
        return "ok"

    bg.submit(_work, kind="sql", title="q")
    await _settle()  # the job completes

    out = await registry.dispatch(
        "background_cancel", {"job_id": "job_001"}, spawn=_spawn_present, background=bg
    )
    assert out["state"] == "completed"
    assert out["cancelled"] is False  # nothing was transitioned


# --------------------------------------------------------------------------- #
# Root-only gating.
# --------------------------------------------------------------------------- #


async def test_background_tools_refused_without_spawn_wiring(tmp_path: Path) -> None:
    # A subagent (no spawn wiring) is refused at the dispatch backstop, independent
    # of whether a registry was threaded.
    registry = _registry(tmp_path)
    bg = BackgroundJobRegistry(id_factory=_ids())
    status = await registry.dispatch("background_status", {}, spawn=None, background=bg)
    assert "not available to subagents" in status["error"]
    cancel = await registry.dispatch(
        "background_cancel", {"job_id": "x"}, spawn=None, background=bg
    )
    assert "not available to subagents" in cancel["error"]


def test_background_tools_hot_and_hidden_from_children(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    hot = {spec.name for spec in registry.hot_prefix()}
    assert {"background_status", "background_cancel"} <= hot

    root = {d.name for d in alkera_tool_descriptors(registry)}
    child = {d.name for d in alkera_tool_descriptors(registry, allow_background_tools=False)}
    assert {"background_status", "background_cancel"} <= root
    assert "background_status" not in child
    assert "background_cancel" not in child
