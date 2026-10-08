"""Real-`claude`-binary test runner for the Claude Agent harness.

Spins up the shared ``MockAnthropicServer`` (Anthropic Messages SSE) and points a
real ``claude`` CLI at it via ``ANTHROPIC_BASE_URL`` (no gateway needed), then
yields a started ``ClaudeAgentAdapter``. Deterministic — the mock scripts every
turn, so no remote LLM, no real network. Parallel to ``opencode_runner.py``.

Auto-skips if no ``claude`` binary is resolvable (the SDK bundles one, so this is
rare — only a stripped install).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from _mocks.mock_anthropic_server import MockAnthropicServer, MockScript
from alkera_cli.harness.adapter import SessionConfig
from alkera_cli.harness.adapters.claude_agent import ClaudeAgentAdapter
from alkera_cli.harness.claude_binary import (
    ClaudeBinaryNotFoundError,
    resolve_claude_binary,
)
from alkera_cli.harness.event_bus import EventBus


def _resolve_or_skip() -> Any:
    # Pinned binary only (env → Nuitka → staged → SDK-bundled; NO $PATH). The
    # SDK wheel bundles a claude, so this resolves in any `uv sync`'d checkout.
    try:
        return resolve_claude_binary()
    except ClaudeBinaryNotFoundError:  # pragma: no cover - only on a stripped install
        pytest.skip("no pinned claude binary resolvable")


def _claude_env(base_url: str) -> dict[str, str]:
    """Point the CLI at the mock with a fake key.

    NOTE: Claude Code verifies model ACCESS (needs a real key), so against the
    mock it can't run a real turn — it returns a well-formed assistant *error*
    turn ("issue with the selected model"). That's fine: these deterministic
    tests validate the ADAPTER's IR-pipeline contract against the real binary
    (spawn → pump → translate → clean close + streaming closure), which that turn
    fully exercises. Real streamed content + the real tool→permission flow are
    covered by the live tests (`-m live_provider`)."""
    return {
        "ANTHROPIC_BASE_URL": base_url,  # http://127.0.0.1:<port>; CLI appends /v1/messages
        "ANTHROPIC_API_KEY": "test-key-not-used",
        "ANTHROPIC_MODEL": "claude-3-5-haiku-20241022",
    }


@asynccontextmanager
async def claude_e2e_adapter(
    tmp_path: Path,
    *,
    mock_script: MockScript,
    session_id: str = "11111111-1111-4111-8111-111111111111",
    chat_dir: Path | None = None,
    harness_native: dict[str, Any] | None = None,
    model: dict[str, str] | None = None,
) -> AsyncIterator[tuple[ClaudeAgentAdapter, MockAnthropicServer]]:
    """Mock Anthropic server + a started ClaudeAgentAdapter wired to it."""
    binary = _resolve_or_skip()
    server = MockAnthropicServer(mock_script)
    await server.start()
    try:
        native: dict[str, Any] = {"claude_env": _claude_env(server.base_url)}
        if harness_native:
            native.update(harness_native)
        resolved_chat_dir = chat_dir or (tmp_path / "chat")
        resolved_chat_dir.mkdir(parents=True, exist_ok=True)
        config = SessionConfig(
            session_id=session_id,
            project_dir=tmp_path,
            chat_dir=resolved_chat_dir,
            harness_native=native,
            model=model,
        )
        adapter = ClaudeAgentAdapter(config, binary=binary, event_bus=EventBus())
        await adapter.start()
        try:
            yield adapter, server
        finally:
            await adapter.stop()
    finally:
        await server.stop()


__all__ = ["claude_e2e_adapter"]
