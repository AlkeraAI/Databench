"""OpenCode adapter: materializing the user's GLOBAL instructions into the agent
config root and threading the absolute path into `config.instructions[]`.

All exercised on a bare adapter — no CLI spawn. The end-to-end proof that the path
reaches the model lives in `test_opencode_instructions_e2e.py`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from alkera_cli.harness.adapter import SessionConfig
from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.opencode_binary import ResolvedOpencodeBinary
from alkera_cli.host import paths


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    # The instructions file now lives under ALKERA_HOME — keep it off the real one.
    monkeypatch.setattr(paths, "ALKERA_HOME", tmp_path / "alkera-home")
    yield


def _adapter(tmp_path: Path, *, harness_native: dict | None = None) -> OpencodeHttpAdapter:
    config = SessionConfig(
        session_id="sid",
        project_dir=tmp_path,
        chat_dir=tmp_path / "chat",
        harness_native=harness_native or {},
    )
    binary = ResolvedOpencodeBinary(
        path=Path("/usr/bin/true"), prefix_args=(), source="path", ripgrep_path=None
    )
    adapter = OpencodeHttpAdapter(config, binary=binary, event_bus=EventBus())
    adapter._harness_dir.mkdir(parents=True, exist_ok=True)
    return adapter


def test_materializes_outside_the_project_and_appends_absolute_path(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path, harness_native={"global_instructions": "GI_MARKER global rules"})
    config: dict = {}
    adapter._add_global_instructions(config)

    assert config["instructions"], "expected the global instructions path appended"
    gi_path = Path(config["instructions"][-1])
    assert gi_path.is_absolute()
    # In the agent config root — never a ~/ path that would mis-resolve under the
    # adapter's home redirect, and never inside the project, where the agent's own
    # write tool could rewrite the instructions it gets handed next turn.
    assert str(adapter._agent_config_root) in str(gi_path)
    assert not gi_path.resolve().is_relative_to(tmp_path.resolve() / "chat")
    assert gi_path.read_text(encoding="utf-8") == "GI_MARKER global rules"


def test_appends_after_existing_instructions(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path, harness_native={"global_instructions": "GI"})
    config: dict = {"instructions": ["/repo/custom.md"]}
    adapter._add_global_instructions(config)

    assert config["instructions"][0] == "/repo/custom.md"  # caller's entry kept first
    assert len(config["instructions"]) == 2  # ours appended last


def test_noop_when_no_global_instructions(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    config: dict = {}
    adapter._add_global_instructions(config)
    assert "instructions" not in config


def test_noop_when_global_instructions_blank(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path, harness_native={"global_instructions": "  \n  "})
    config: dict = {}
    adapter._add_global_instructions(config)
    assert "instructions" not in config


def test_build_env_serializes_instructions_into_config_content(tmp_path: Path) -> None:
    """The whole seam: ALKERA_CONFIG_CONTENT carries the materialized path so opencode
    reads it via config.instructions[]."""
    adapter = _adapter(tmp_path, harness_native={"global_instructions": "GI_MARKER"})
    config = json.loads(adapter._build_env("test-password").secrets["ALKERA_CONFIG_CONTENT"])
    assert any("global-instructions.md" in str(p) for p in config.get("instructions", []))
