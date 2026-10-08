"""Permission parity for the Claude Agent adapter.

The adapter keeps Claude Code in ``permission_mode="default"`` and bridges the
SDK's ``can_use_tool`` callback to OUR broker: it publishes a ``PermissionRequest``
(with the harness-agnostic ``canonical_kind``) and blocks on a future that
``resolve_permission`` completes. The orchestrator's ``mode_auto_decision`` (the
caller-supplied resolver) is what turns the active ``PermissionMode`` into
allow/reject — exactly like opencode. These tests prove that bridge + the
canonical mapping + the full mode x kind matrix.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.harness.adapter import SessionConfig
from alkera_cli.harness.adapters.claude_agent import (
    ClaudeAgentAdapter,
    _cc_tool_to_canonical,
)
from alkera_cli.harness.claude_binary import ResolvedClaudeBinary
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.permission_mode import PermissionMode, mode_auto_decision
from alkera_core.schemas.chat import CanonicalPermissionKind, PermissionRequest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny


class _Ctx:
    """Minimal stand-in for the SDK's ToolPermissionContext."""

    def __init__(self, tool_use_id: str | None = None) -> None:
        self.tool_use_id = tool_use_id
        self.suggestions: list[Any] = []
        self.signal = None


def _adapter(tmp_path: Path) -> ClaudeAgentAdapter:
    config = SessionConfig(session_id="our-sid", project_dir=tmp_path, chat_dir=tmp_path / "chat")
    binary = ResolvedClaudeBinary(path=Path("/usr/bin/true"), source="path")
    return ClaudeAgentAdapter(config, binary=binary, event_bus=EventBus())


async def _next(sub: object, kind: type, *, timeout_s: float = 1.0) -> Any:
    async def _pull() -> Any:
        async for ev in sub:  # type: ignore[attr-defined]
            if isinstance(ev, kind):
                return ev
        return None

    return await asyncio.wait_for(_pull(), timeout=timeout_s)


async def _run_gate(
    adapter: ClaudeAgentAdapter,
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    tool_use_id: str | None,
    option_id: str,
) -> tuple[PermissionResultAllow | PermissionResultDeny, PermissionRequest]:
    """Drive one can_use_tool round-trip: spawn the callback, capture the
    PermissionRequest, reply with ``option_id``, return (result, request)."""
    sub = adapter._bus.subscribe()
    task = asyncio.create_task(adapter._can_use_tool(tool_name, tool_input, _Ctx(tool_use_id)))
    req: PermissionRequest = await _next(sub, PermissionRequest)
    await adapter.resolve_permission(req.request_id, option_id)
    result = await asyncio.wait_for(task, timeout=1.0)
    return result, req


# ---------------------------------------------------------------------------
# Canonical kind mapping
# ---------------------------------------------------------------------------


def test_canonical_kind_mapping() -> None:
    assert _cc_tool_to_canonical("Bash") == "shell"
    for edit_tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        assert _cc_tool_to_canonical(edit_tool) == "edit"
    assert _cc_tool_to_canonical("Task") == "task"
    assert _cc_tool_to_canonical("WebFetch") == "network"
    assert _cc_tool_to_canonical("WebSearch") == "network"
    assert _cc_tool_to_canonical("Read") == "other"
    assert _cc_tool_to_canonical("mcp__plan__present_plan") == "other"
    assert _cc_tool_to_canonical("SomethingNew") == "other"


# ---------------------------------------------------------------------------
# can_use_tool ↔ resolve_permission bridge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsupported_tool_auto_denied_without_prompt(tmp_path: Path) -> None:
    """A tool that isn't one of our gated primitives (a vendor tool that slipped
    past the disallow-list — e.g. AskUserQuestion or a newly-shipped one) is
    auto-denied, and NO PermissionRequest is parked → the user is never prompted."""
    adapter = _adapter(tmp_path)
    for tool in ("AskUserQuestion", "Task", "TodoWrite", "SomeFutureVendorTool"):
        result = await adapter._can_use_tool(tool, {}, _Ctx("tu"))
        assert isinstance(result, PermissionResultDeny), tool
    assert adapter._pending_permissions == {}  # nothing parked → no prompt fired


@pytest.mark.asyncio
async def test_web_tools_route_through_broker(tmp_path: Path) -> None:
    """WebFetch/WebSearch are re-enabled + gated (canonical 'network'): they reach
    the broker as a PermissionRequest — NOT auto-denied by the _GATED_TOOLS guard."""
    adapter = _adapter(tmp_path)
    for tool in ("WebFetch", "WebSearch"):
        result, req = await _run_gate(
            adapter,
            tool,
            {"url": "https://example.com"},
            tool_use_id=f"t-{tool}",
            option_id="allow_once",
        )
        assert isinstance(result, PermissionResultAllow), tool
        assert req.permission_kind == tool
        assert req.canonical_kind == "network"


@pytest.mark.asyncio
async def test_request_carries_native_and_canonical_kind(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    _result, req = await _run_gate(
        adapter, "Bash", {"command": "ls"}, tool_use_id="tu1", option_id="allow_once"
    )
    assert req.permission_kind == "Bash"
    assert req.canonical_kind == "shell"
    assert req.tool_call_id == "tu1"
    assert req.request_id == "tu1"
    assert {o.option_id for o in req.options} == {
        "allow_once",
        "allow_always",
        "reject_once",
        "reject_always",
    }


@pytest.mark.asyncio
async def test_read_tools_now_reach_the_broker(tmp_path: Path) -> None:
    """Full passthrough: Read/Glob/Grep/LS are no longer vendor-auto-allowed —
    they reach can_use_tool with an (fs, read) subject the runtime policy then
    auto-allows. (Pre-flip they never hit the callback.)"""
    adapter = _adapter(tmp_path)
    for tool in ("Read", "Glob", "Grep", "LS"):
        result, req = await _run_gate(
            adapter, tool, {"file_path": "/x"}, tool_use_id=f"t-{tool}", option_id="allow_once"
        )
        assert isinstance(result, PermissionResultAllow), tool
        assert req.subject is not None, tool
        assert req.subject["capability"] == "fs"
        assert req.subject["effect"] == "read"


@pytest.mark.asyncio
async def test_bash_request_carries_classified_subject(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    _r, safe = await _run_gate(
        adapter, "Bash", {"command": "ls -la"}, tool_use_id="t1", option_id="allow_once"
    )
    assert safe.subject is not None
    assert safe.subject["capability"] == "shell"
    assert safe.subject["effect"] == "read"
    assert safe.patterns == ["ls -la"]

    _r2, danger = await _run_gate(
        adapter, "Bash", {"command": "rm -rf /"}, tool_use_id="t2", option_id="reject_once"
    )
    assert danger.subject is not None
    assert danger.subject["effect"] == "destroy"


@pytest.mark.asyncio
async def test_edit_request_carries_fs_write_subject(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    _r, req = await _run_gate(
        adapter, "Edit", {"file_path": "/x"}, tool_use_id="t", option_id="allow_once"
    )
    assert req.subject is not None
    assert req.subject["capability"] == "fs"
    assert req.subject["effect"] == "write"


@pytest.mark.asyncio
async def test_allow_once_returns_allow_with_input(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    result, _ = await _run_gate(
        adapter, "Edit", {"path": "/x"}, tool_use_id="t", option_id="allow_once"
    )
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input == {"path": "/x"}


@pytest.mark.asyncio
async def test_allow_always_returns_allow(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    result, _ = await _run_gate(adapter, "Edit", {}, tool_use_id="t", option_id="allow_always")
    assert isinstance(result, PermissionResultAllow)


@pytest.mark.asyncio
async def test_reject_returns_deny(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    for opt in ("reject_once", "reject_always", "cancelled"):
        result, _ = await _run_gate(adapter, "Bash", {}, tool_use_id=f"t-{opt}", option_id=opt)
        assert isinstance(result, PermissionResultDeny)


@pytest.mark.asyncio
async def test_missing_tool_use_id_generates_request_id(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    _result, req = await _run_gate(adapter, "Bash", {}, tool_use_id=None, option_id="allow_once")
    assert req.request_id  # non-empty generated id
    assert req.tool_call_id is None


@pytest.mark.asyncio
async def test_pending_future_cleaned_up(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    await _run_gate(adapter, "Bash", {}, tool_use_id="t", option_id="allow_once")
    assert adapter._pending_permissions == {}


# ---------------------------------------------------------------------------
# The full mode x kind matrix — parity proof (CC stays in "default" throughout;
# OUR mode_auto_decision drives every gate via the bridge).
# ---------------------------------------------------------------------------


def _decision_to_option(decision: str) -> str:
    # The CLI/daemon resolver maps an auto-decision to an option; a "prompt" is
    # what the user sees — here we model the user choosing allow.
    return {"allow": "allow_once", "reject": "reject_once", "prompt": "allow_once"}[decision]


@pytest.mark.asyncio
async def test_mode_matrix_parity(tmp_path: Path) -> None:
    cases: list[tuple[PermissionMode, CanonicalPermissionKind, str, type]] = [
        ("bypass", "edit", "allow", PermissionResultAllow),
        ("bypass", "shell", "allow", PermissionResultAllow),
        ("auto", "edit", "allow", PermissionResultAllow),
        ("auto", "shell", "allow", PermissionResultAllow),
        ("plan", "edit", "reject", PermissionResultDeny),
        ("plan", "shell", "prompt", PermissionResultAllow),
        ("default", "edit", "prompt", PermissionResultAllow),
        ("default", "shell", "prompt", PermissionResultAllow),
    ]
    tool_for = {"edit": "Edit", "shell": "Bash"}
    for mode, kind, expected_decision, expected_result in cases:
        adapter = _adapter(tmp_path)
        decision = mode_auto_decision(mode, kind)
        assert decision == expected_decision, f"{mode}/{kind}"
        option = _decision_to_option(decision)
        result, req = await _run_gate(
            adapter, tool_for[kind], {}, tool_use_id="t", option_id=option
        )
        assert req.canonical_kind == kind
        assert isinstance(result, expected_result), f"{mode}/{kind}"


@pytest.mark.asyncio
async def test_reject_reason_becomes_the_deny_message(tmp_path: Path) -> None:
    # A reject's reason (the auto-mode judge's verdict / the policy's reasons)
    # must reach the MODEL as the denial message — not a generic "rejected".
    adapter = _adapter(tmp_path)
    sub = adapter._bus.subscribe()
    task = asyncio.create_task(adapter._can_use_tool("Bash", {"command": "x"}, _Ctx("t1")))
    req: PermissionRequest = await _next(sub, PermissionRequest)
    await adapter.resolve_permission(
        req.request_id, "reject_once", reason="exfiltrates data to a remote host"
    )
    result = await asyncio.wait_for(task, timeout=1.0)
    assert isinstance(result, PermissionResultDeny)
    assert result.message == "exfiltrates data to a remote host"


@pytest.mark.asyncio
async def test_a_reject_without_reason_is_a_person_s_and_not_called_policy(tmp_path: Path) -> None:
    """Only a person's reject reaches the adapter with no reason (the runtime
    gives every policy refusal one), so the fallback names no policy."""
    adapter = _adapter(tmp_path)
    result, _req = await _run_gate(adapter, "Bash", {}, tool_use_id="t2", option_id="reject_once")
    assert isinstance(result, PermissionResultDeny)
    assert result.message == "Permission was not granted for this call."
