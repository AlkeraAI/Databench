"""Plan-approval bridge: the in-process ``present_plan`` SDK-MCP tool drives the
existing ``QuestionRequest(kind="plan_approval")`` surface (no CC native plan
mode). The tool handler publishes the question, blocks, and ``answer_question`` /
``reject_question`` resolve it — returning the verdict text to the model.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.harness.adapter import SessionConfig
from alkera_cli.harness.adapters.claude_agent import ClaudeAgentAdapter
from alkera_cli.harness.claude_binary import ResolvedClaudeBinary
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.permission_mode import PLAN_ACCEPT_OPTIONS
from alkera_core.schemas.chat import QuestionRequest


def _adapter(tmp_path: Path) -> ClaudeAgentAdapter:
    config = SessionConfig(session_id="our-sid", project_dir=tmp_path, chat_dir=tmp_path / "chat")
    return ClaudeAgentAdapter(
        config,
        binary=ResolvedClaudeBinary(path=Path("/usr/bin/true"), source="path"),
        event_bus=EventBus(),
    )


def _write_plan(adapter: ClaudeAgentAdapter, content: str = "step 1\nstep 2") -> str:
    """Write a plan file to the chat sandbox (file-based plan mode) and return the
    path the model would pass to present_plan."""
    sandbox = adapter._config.chat_dir / "sandbox"
    sandbox.mkdir(parents=True, exist_ok=True)
    (sandbox / "plan.md").write_text(content)
    return "plan.md"


async def _next_question(sub: object, *, timeout_s: float = 1.0) -> QuestionRequest:
    async def _pull() -> QuestionRequest:
        async for ev in sub:  # type: ignore[attr-defined]
            if isinstance(ev, QuestionRequest):
                return ev
        raise AssertionError("stream ended before a QuestionRequest")

    return await asyncio.wait_for(_pull(), timeout=timeout_s)


async def test_present_plan_tool_is_clearly_described(tmp_path: Path) -> None:
    """The custom MCP tool's spec is all the model has to go on (it wasn't trained
    on it), so it must carry a real contract: a description covering approval AND
    rejection, plus a DOCUMENTED `path` parameter (file-based plan mode)."""
    from mcp.types import ListToolsRequest

    srv = _adapter(tmp_path)._make_plan_server()
    handler = srv["instance"].request_handlers[ListToolsRequest]
    res = await handler(ListToolsRequest(method="tools/list"))
    tools = {t.name: t for t in res.root.tools}
    assert "present_plan" in tools
    pp = tools["present_plan"]
    desc = (pp.description or "").lower()
    assert "approv" in desc and "reject" in desc  # the call's return contract
    path = pp.inputSchema["properties"]["path"]
    assert path["type"] == "string"
    assert len(path.get("description", "")) > 20  # the parameter is actually documented


@pytest.mark.asyncio
async def test_present_plan_emits_plan_approval_question(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    sub = adapter._bus.subscribe()
    path = _write_plan(adapter, "step 1\nstep 2")
    task = asyncio.create_task(adapter._handle_present_plan(path))
    req = await _next_question(sub)
    assert req.kind == "plan_approval"
    # The plan FILE's content is surfaced on the event for the UI (not re-fed to the model).
    assert req.plan_markdown == "step 1\nstep 2"
    assert len(req.questions) == 1
    labels = [o.label for o in req.questions[0].options]
    assert labels == [label for label, _ in PLAN_ACCEPT_OPTIONS]
    # resolve so the task completes
    await adapter.answer_question(req.request_id, [[labels[0]]])
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_answer_returns_approved_text(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    sub = adapter._bus.subscribe()
    task = asyncio.create_task(adapter._handle_present_plan(_write_plan(adapter, "the plan")))
    req = await _next_question(sub)
    chosen = PLAN_ACCEPT_OPTIONS[1][0]  # "Accept — auto-accept edits ..."
    await adapter.answer_question(req.request_id, [[chosen]])
    result: dict[str, Any] = await asyncio.wait_for(task, timeout=1.0)
    text = result["content"][0]["text"]
    assert "approved" in text.lower()
    assert adapter._pending_questions == {}


@pytest.mark.asyncio
async def test_custom_text_answer_rejects_plan(tmp_path: Path) -> None:
    """Free-form custom text (an `=...` answer, NOT one of the Accept options) is
    feedback → a rejection the model should revise against, not an approval. The
    answer arrives as a normal `answer` payload, so it's the label↔mode lookup
    (plan_label_to_mode → None) that distinguishes it. Parity with opencode."""
    adapter = _adapter(tmp_path)
    sub = adapter._bus.subscribe()
    task = asyncio.create_task(adapter._handle_present_plan(_write_plan(adapter, "the plan")))
    req = await _next_question(sub)
    await adapter.answer_question(req.request_id, [["use Rust instead"]])  # custom text
    result: dict[str, Any] = await asyncio.wait_for(task, timeout=1.0)
    text = result["content"][0]["text"]
    assert "rejected" in text.lower()
    assert "use Rust instead" in text  # the feedback is echoed back to the model
    assert "approved" not in text.lower()
    assert adapter._pending_questions == {}


@pytest.mark.asyncio
async def test_reject_returns_rejection_text_with_reason(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    sub = adapter._bus.subscribe()
    task = asyncio.create_task(adapter._handle_present_plan(_write_plan(adapter, "the plan")))
    req = await _next_question(sub)
    await adapter.reject_question(req.request_id, "too risky")
    result: dict[str, Any] = await asyncio.wait_for(task, timeout=1.0)
    text = result["content"][0]["text"]
    assert "rejected" in text.lower()
    assert "too risky" in text
    assert adapter._pending_questions == {}


@pytest.mark.asyncio
async def test_reject_without_reason_has_fallback(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    sub = adapter._bus.subscribe()
    task = asyncio.create_task(adapter._handle_present_plan(_write_plan(adapter, "p")))
    req = await _next_question(sub)
    await adapter.reject_question(req.request_id, None)
    result = await asyncio.wait_for(task, timeout=1.0)
    assert "no reason given" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_present_plan_without_a_file_steers_the_model(tmp_path: Path) -> None:
    """No plan file at the path → no approval question; a short result tells the
    model to write the plan to its sandbox first (it doesn't block)."""
    adapter = _adapter(tmp_path)
    result = await adapter._handle_present_plan("does-not-exist.md")
    text = result["content"][0]["text"].lower()
    assert "no plan found" in text
    assert "write your plan" in text
    assert adapter._pending_questions == {}


def test_sandbox_writes_are_auto_allowed(tmp_path: Path) -> None:
    """A Write/Edit whose target is inside the chat sandbox is recognized so
    can_use_tool auto-allows it (incl. plan.md in plan mode); a project file is not."""
    adapter = _adapter(tmp_path)
    sandbox = adapter._config.chat_dir / "sandbox"
    assert adapter._is_sandbox_path({"file_path": str(sandbox / "plan.md")}) is True
    assert adapter._is_sandbox_path({"file_path": str(sandbox / "sub" / "data.parquet")}) is True
    assert adapter._is_sandbox_path({"file_path": str(tmp_path / "src" / "main.py")}) is False
    assert adapter._is_sandbox_path({}) is False
