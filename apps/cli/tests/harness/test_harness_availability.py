"""``HarnessAdapter.is_available()`` — the harness-availability gate the runtime/UI
consults before offering a harness. opencode is always available (we bundle it);
the Claude-agent harness only when a local ``claude`` is installed; the ABC
default is "available"; the factory dispatches by ``harness_type``.
"""

from __future__ import annotations

import pytest
from alkera_cli.harness import claude_binary
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.adapters.claude_agent import ClaudeAgentAdapter
from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
from alkera_cli.harness.runtime import AdapterFactory


def test_abc_default_is_available() -> None:
    # FakeAdapter doesn't override is_available → inherits the ABC default (True).
    assert FakeAdapter.is_available() is True


def test_opencode_always_available() -> None:
    assert OpencodeHttpAdapter.is_available() is True


def test_claude_available_in_dev_env() -> None:
    # A uv-synced env has the claude-agent-sdk bundled CLI → discoverable.
    assert ClaudeAgentAdapter.is_available() is True


def test_claude_unavailable_without_local_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALKERA_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(claude_binary.shutil, "which", lambda _name: None)
    monkeypatch.setattr(claude_binary, "_known_locations", list)
    monkeypatch.setattr(claude_binary, "_sdk_bundled_path", lambda: None)
    assert ClaudeAgentAdapter.is_available() is False


def test_factory_is_available_dispatch() -> None:
    factory = AdapterFactory()
    assert factory.is_available("agent") is True
    assert factory.is_available("claude-agent") is True  # dev SDK bundle present
    assert factory.is_available("bogus-harness") is False
