"""The opencode subprocess is told whether this deployment serves delegation.

Withholding Alkera's own `spawn_agent` / `list_agent_types` is only half the job:
opencode ships its OWN spawner (`task`), and a model offered it delegates into a
child session the product has no surface for. The vendored `tool/registry.ts`
drops `task` from the ADVERTISED set when this flag says delegation is off; these
tests pin the Python half — that the flag is carried from the session's config
(not this process's env) and is set in both directions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.harness.adapter import SessionConfig
from alkera_cli.harness.adapters.opencode_http import (
    _OPENCODE_PERMISSION_ASK,
    _OPENCODE_SUBAGENTS_FLAG,
    OpencodeHttpAdapter,
)
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.opencode_binary import ResolvedOpencodeBinary
from alkera_cli.harness.runtime import HarnessRuntime
from alkera_cli.host.config import CliSettings

_GATEWAY_CONFIG: dict[str, Any] = {
    "provider": {"mock": {"npm": "@ai-sdk/openai-compatible", "options": {"baseURL": "http://x"}}},
    "model": "mock/mock-model",
}


def _env(tmp_path: Path, **kwargs: Any) -> dict[str, str]:
    config = SessionConfig(
        session_id="our-sid",
        project_dir=tmp_path,
        chat_dir=tmp_path / "chat",
        harness_native={"agent_config": dict(_GATEWAY_CONFIG)},
        **kwargs,
    )
    binary = ResolvedOpencodeBinary(path=Path("/usr/bin/true"), prefix_args=(), source="staged")
    return OpencodeHttpAdapter(config, binary=binary, event_bus=EventBus())._build_env("pw").env


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        pytest.param(True, "true", id="delegation-on"),
        pytest.param(False, "false", id="delegation-off"),
    ],
)
def test_the_child_env_carries_the_session_decision(
    tmp_path: Path, configured: bool, expected: str
) -> None:
    """Whatever the session was configured with reaches the subprocess verbatim."""
    assert _env(tmp_path, subagents_enabled=configured)[_OPENCODE_SUBAGENTS_FLAG] == expected


def test_a_session_that_says_nothing_keeps_delegation(tmp_path: Path) -> None:
    """The default is on — turning delegation off is a deployment's explicit choice,
    never something a caller falls into by omitting a field."""
    assert _env(tmp_path)[_OPENCODE_SUBAGENTS_FLAG] == "true"


def test_an_inherited_flag_cannot_decide_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stray `ALKERA_SUBAGENTS_ENABLED=false` in the user's own shell must not
    silently strip the task tool from a session that serves delegation — the
    session's config is the only input."""
    monkeypatch.setenv(_OPENCODE_SUBAGENTS_FLAG, "false")
    assert _env(tmp_path, subagents_enabled=True)[_OPENCODE_SUBAGENTS_FLAG] == "true"


def test_the_call_time_deny_survives_the_registry_removal(tmp_path: Path) -> None:
    """The permission deny stays on both settings. It is the floor under the
    registry removal: a per-agent ruleset merged after ours could re-advertise
    `task` on a deployment that serves delegation, and a box still running an
    older staged binary has no registry filter at all — in both cases the deny is
    what refuses the call."""
    assert _OPENCODE_PERMISSION_ASK["task"] == "deny"
    for configured in (True, False):
        permission = json.loads(_env(tmp_path, subagents_enabled=configured)["ALKERA_PERMISSION"])
        assert permission["task"] == "deny", permission


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        pytest.param(True, "true", id="setting-on"),
        pytest.param(False, "false", id="setting-off"),
    ],
)
def test_the_setting_reaches_the_child_through_the_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: bool, expected: str
) -> None:
    """The whole chain, driven end to end without a subprocess: the
    `ALKERA_SUBAGENTS_ENABLED` setting → the runtime → `SessionConfig` → the child
    env. Spelling the two ends independently is exactly how this would stop
    firing, so read the runtime's own answer rather than restating the setting."""
    monkeypatch.setattr(
        "alkera_cli.host.config.get_settings",
        lambda: CliSettings(alkera_subagents_enabled=setting),
    )
    runtime = HarnessRuntime.__new__(HarnessRuntime)
    runtime._subagents_enabled = None  # type: ignore[attr-defined]
    assert runtime.subagents_enabled is setting
    assert (
        _env(tmp_path, subagents_enabled=runtime.subagents_enabled)[_OPENCODE_SUBAGENTS_FLAG]
        == expected
    )
