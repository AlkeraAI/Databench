"""The agent the opencode e2e suite spawns carries an `rg`, so no tool call
waits on a download.

Without one, opencode fetches ripgrep from github.com inside the first glob or
grep, into a cache under the per-test agent root that is deleted when the agent
stops, so every such test downloaded it again. On the Windows e2e job (which
points `ALKERA_OPENCODE_BIN` at the compiled agent, with `rg.exe` staged beside
it) a slow download timed the test out and a refused one failed the tool:
``glob never completed (saw ['running', 'error'])``.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from _helpers.opencode_runner import _bun_binary
from alkera_cli.harness.opencode_binary import _opencode_filename, _ripgrep_filename


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_a_compiled_agent_override_spawns_with_the_rg_staged_beside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    agent = _executable(stage / _opencode_filename())
    rg = _executable(stage / _ripgrep_filename())
    empty_path = tmp_path / "no-rg-here"
    empty_path.mkdir()
    monkeypatch.setenv("ALKERA_OPENCODE_BIN", str(agent))
    monkeypatch.delenv("ALKERA_RIPGREP_BIN", raising=False)
    # The CI runner has no system rg; neither does this PATH.
    monkeypatch.setenv("PATH", str(empty_path))

    binary = _bun_binary()

    assert binary.path == agent.absolute()
    assert binary.ripgrep_path == rg.absolute()


def test_an_rg_the_caller_names_is_the_one_spawned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    agent = _executable(stage / _opencode_filename())
    _executable(stage / _ripgrep_filename())
    wrapper = _executable(tmp_path / _ripgrep_filename())
    monkeypatch.setenv("ALKERA_OPENCODE_BIN", str(agent))

    assert _bun_binary(ripgrep_path=wrapper).ripgrep_path == wrapper
