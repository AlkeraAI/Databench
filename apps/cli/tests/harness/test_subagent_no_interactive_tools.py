"""A subagent must never be surfaced an INTERACTIVE tool — plan-approval or
ask-the-user. A subagent runs headless (no human watches it), so a tool that
blocks on a human verdict would deadlock it. This pins, for BOTH backends and
BOTH tool sources (vendor-native + Alkera in-process MCP):

* Claude SDK — the in-process ``present_plan`` / ``present_question`` MCP servers
  are NOT registered for a subagent (so the model never sees the tools), their
  allow-list entries are dropped, and the ask-tool steering is removed. The main
  session keeps all three. (Claude's native EnterPlanMode/ExitPlanMode/
  AskUserQuestion are already globally disallowed.)
* opencode — the native ``plan_present`` / ``plan_exit`` / ``question`` tools are
  set to ``deny`` for a subagent, which opencode's ``Permission.disabled()``
  strips from the toolset before the model sees them (same mechanism as the
  existing ``task``/``todowrite``/``skill`` denies).
* Defense-in-depth — even if a call slips through (a resumed transcript), the
  Claude handlers short-circuit with an error and publish NO ``QuestionRequest``,
  so the human surface is never engaged and nothing hangs.

The working toolset (Bash/Edit/Read/… and the non-interactive ``alkera_*`` plugin
surface) is untouched — a subagent must still do real work.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import pytest
from alkera_cli.harness.adapter import PromptInput, SessionConfig
from alkera_cli.harness.adapters.claude_agent import (
    _ASK_SERVER,
    _ASK_TOOL_FQN,
    _CLAUDE_PLAN_STEERING,
    _PLAN_SERVER,
    _PLAN_TOOL_FQN,
    ClaudeAgentAdapter,
)
from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
from alkera_cli.harness.claude_binary import ResolvedClaudeBinary
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.opencode_binary import ResolvedOpencodeBinary
from alkera_core.schemas.chat import QuestionRequest


def _claude_adapter(tmp_path: Path, *, subagent: bool) -> ClaudeAgentAdapter:
    config = SessionConfig(
        session_id="our-sid",
        project_dir=tmp_path,
        chat_dir=tmp_path / "chat",
        parent_session_id="parent-sid" if subagent else None,
    )
    return ClaudeAgentAdapter(
        config,
        binary=ResolvedClaudeBinary(path=Path("/usr/bin/true"), source="path"),
        event_bus=EventBus(),
    )


def _opencode_adapter(tmp_path: Path, *, subagent: bool) -> OpencodeHttpAdapter:
    config = SessionConfig(
        session_id="sid",
        project_dir=tmp_path,
        chat_dir=tmp_path / "chat",
        parent_session_id="parent-sid" if subagent else None,
    )
    binary = ResolvedOpencodeBinary(
        path=Path("/usr/bin/true"), prefix_args=(), source="path", ripgrep_path=None
    )
    adapter = OpencodeHttpAdapter(config, binary=binary, event_bus=EventBus())
    adapter._harness_dir.mkdir(parents=True, exist_ok=True)
    return adapter


async def _next_question(sub: object, *, timeout_s: float = 1.0) -> QuestionRequest:
    async def _pull() -> QuestionRequest:
        async for ev in sub:  # type: ignore[attr-defined]
            if isinstance(ev, QuestionRequest):
                return ev
        raise AssertionError("stream ended before a QuestionRequest")

    return await asyncio.wait_for(_pull(), timeout=timeout_s)


async def _assert_no_question(sub: object, *, timeout_s: float = 0.2) -> None:
    """A subagent handler must short-circuit BEFORE publishing — so no
    ``QuestionRequest`` ever reaches the bus (the human surface is never engaged,
    nothing blocks)."""
    with pytest.raises(asyncio.TimeoutError):
        await _next_question(sub, timeout_s=timeout_s)


# ---------------------------------------------------------------------------
# Claude — interactive tools not surfaced to a subagent
# ---------------------------------------------------------------------------


def test_claude_main_surfaces_plan_and_ask(tmp_path: Path) -> None:
    opts = _claude_adapter(tmp_path, subagent=False)._build_options(resume=False)
    assert _PLAN_SERVER in opts.mcp_servers
    assert _ASK_SERVER in opts.mcp_servers
    allow = json.loads(opts.settings)["permissions"]["allow"]
    assert _PLAN_TOOL_FQN in allow
    assert _ASK_TOOL_FQN in allow
    # main agent is steered toward the ask tool
    assert _ASK_TOOL_FQN in opts.system_prompt["append"]


def test_claude_subagent_hides_plan_and_ask(tmp_path: Path) -> None:
    opts = _claude_adapter(tmp_path, subagent=True)._build_options(resume=False)
    # not advertised to the model at all
    assert _PLAN_SERVER not in opts.mcp_servers
    assert _ASK_SERVER not in opts.mcp_servers
    # no dangling allow-list entries for the absent tools
    allow = json.loads(opts.settings)["permissions"]["allow"]
    assert allow == []
    assert _PLAN_TOOL_FQN not in allow
    assert _ASK_TOOL_FQN not in allow
    # and no steering toward a tool that no longer exists
    assert opts.system_prompt["append"] == ""
    assert _ASK_TOOL_FQN not in opts.system_prompt["append"]


def test_claude_subagent_keeps_working_toolset(tmp_path: Path) -> None:
    """The fix removes ONLY the interactive tools — the real toolset still flows
    through the broker (full passthrough), so a subagent can still do work."""
    opts = _claude_adapter(tmp_path, subagent=True)._build_options(resume=False)
    ask = json.loads(opts.settings)["permissions"]["ask"]
    for tool in ("Bash", "Edit", "Read", "Glob", "Grep"):
        assert tool in ask, tool


# ---------------------------------------------------------------------------
# Claude — handler guards (deadlock-proof even if a call slips through)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_subagent_present_plan_refused_without_blocking(tmp_path: Path) -> None:
    adapter = _claude_adapter(tmp_path, subagent=True)
    sub = adapter._bus.subscribe()
    # Write a REAL plan file: the only reason to refuse must be subagent-ness, not
    # a missing plan.
    sandbox = adapter._config.chat_dir / "sandbox"
    sandbox.mkdir(parents=True, exist_ok=True)
    (sandbox / "plan.md").write_text("step 1\nstep 2")
    result = await asyncio.wait_for(adapter._handle_present_plan("plan.md"), timeout=1.0)
    assert "subagent" in result["content"][0]["text"].lower()
    await _assert_no_question(sub)


@pytest.mark.asyncio
async def test_claude_subagent_present_question_refused_without_blocking(tmp_path: Path) -> None:
    adapter = _claude_adapter(tmp_path, subagent=True)
    sub = adapter._bus.subscribe()
    result = await asyncio.wait_for(
        adapter._handle_present_question([{"question": "Which option?"}]), timeout=1.0
    )
    assert "subagent" in result["content"][0]["text"].lower()
    await _assert_no_question(sub)


@pytest.mark.asyncio
async def test_claude_main_present_question_still_publishes(tmp_path: Path) -> None:
    """Contrast: the guard must NOT fire for the main agent — it still asks."""
    adapter = _claude_adapter(tmp_path, subagent=False)
    sub = adapter._bus.subscribe()
    task = asyncio.create_task(adapter._handle_present_question([{"question": "x?"}]))
    try:
        req = await _next_question(sub)
        assert req.kind == "question"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_claude_subagent_gets_no_plan_steering_even_if_agent_is_plan(tmp_path: Path) -> None:
    """Defense-in-depth: a subagent is never put in plan mode (the runtime
    downgrades a plan ceiling to read_only), but even if a turn were labeled
    agent="plan", the adapter must NOT steer a subagent toward the removed
    present_plan/present_question tools. The main agent still gets the steering."""
    plan_turn = PromptInput(text="do the thing", agent="plan")
    assert _claude_adapter(tmp_path, subagent=True)._plan_steering(plan_turn) is None
    assert (
        _claude_adapter(tmp_path, subagent=False)._plan_steering(plan_turn) == _CLAUDE_PLAN_STEERING
    )


# ---------------------------------------------------------------------------
# opencode — interactive tools denied (→ stripped from the toolset) for a subagent
# ---------------------------------------------------------------------------


def test_opencode_main_allows_interactive_tools(tmp_path: Path) -> None:
    perm = json.loads(
        _opencode_adapter(tmp_path, subagent=False)._build_env("pw").env["ALKERA_PERMISSION"]
    )
    assert perm["plan_present"] == "allow"
    assert perm["question"] == "allow"
    assert perm.get("plan_exit") != "deny"  # absent → falls under the "*": "ask" default


@pytest.mark.parametrize("tool", ["plan_present", "plan_exit", "question"])
def test_opencode_subagent_denies_interactive_tool(tmp_path: Path, tool: str) -> None:
    perm = json.loads(
        _opencode_adapter(tmp_path, subagent=True)._build_env("pw").env["ALKERA_PERMISSION"]
    )
    assert perm[tool] == "deny"


def test_opencode_subagent_keeps_working_toolset(tmp_path: Path) -> None:
    perm = json.loads(
        _opencode_adapter(tmp_path, subagent=True)._build_env("pw").env["ALKERA_PERMISSION"]
    )
    assert perm["*"] == "ask"  # real tools still gated through the broker, not removed
    assert perm["alkera_*"] == "allow"  # non-interactive plugin surface intact
