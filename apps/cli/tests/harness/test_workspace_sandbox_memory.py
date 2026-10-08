"""What a workspace's sandbox is said to hold in memory.

Read from the cgroup each member's container is counted in, less the file
cache the kernel would drop; and from gVisor's own stats where the cgroup
counts nothing (a local box in Docker, where the containers' cgroups stay
empty), which is the reading the box reports to the backend.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.harness.sandbox_layout import chat_cgroup_path, chat_container
from alkera_cli.harness.sandbox_scope import (
    SCOPE_KEY,
    forget_scope,
    register_scope,
    scope_for,
)
from alkera_cli.harness.workspace_sandbox import runsc_usage_bytes, workspace_memory_mb

WS = "ws:8a7c1f2e-0000-4000-8000-000000000001"
MIB = 1024 * 1024


@pytest.fixture(autouse=True)
def _members() -> Iterator[None]:
    for session in ("chat-a", "chat-b"):
        register_scope(scope_for(session, {SCOPE_KEY: WS}, topology="per_chat"))
    yield
    for session in ("chat-a", "chat-b"):
        forget_scope(session)


def _group(root: Path, container: str, *, current: int, inactive: int) -> None:
    group = chat_cgroup_path(container, root)
    group.mkdir(parents=True)
    (group / "memory.current").write_text(f"{current}\n")
    (group / "memory.stat").write_text(f"anon 1\ninactive_file {inactive}\n")


def test_the_members_cgroups_are_summed_less_their_droppable_cache(tmp_path: Path) -> None:
    _group(tmp_path, "chat-a", current=300 * MIB, inactive=100 * MIB)
    _group(tmp_path, "chat-b", current=150 * MIB, inactive=0)

    assert workspace_memory_mb(WS, cgroup_root=tmp_path, runsc_usage=lambda _c: None) == 350


def test_gvisor_answers_where_the_cgroups_count_nothing(tmp_path: Path) -> None:
    _group(tmp_path, "chat-a", current=0, inactive=0)
    asked: list[str] = []

    def usage(container: str) -> int | None:
        asked.append(container)
        return {"chat-a": 440 * MIB, "chat-b": 390 * MIB}[container]

    assert workspace_memory_mb(WS, cgroup_root=tmp_path, runsc_usage=usage) == 830
    assert sorted(asked) == ["chat-a", "chat-b"]


def test_nothing_readable_is_no_reading(tmp_path: Path) -> None:
    assert workspace_memory_mb(WS, cgroup_root=tmp_path, runsc_usage=lambda _c: None) is None


def _run(stdout: str, code: int = 0) -> Any:
    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        assert argv[-3:] == ["events", "-stats", chat_container("chat-a")]
        return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr="")

    return run


def test_runsc_stats_are_read_as_the_sandboxs_usage() -> None:
    stats = {"type": "stats", "data": {"memory": {"usage": {"limit": 0, "usage": 465256448}}}}
    assert runsc_usage_bytes("chat-a", run=_run(json.dumps(stats))) == 465256448


@pytest.mark.parametrize(
    ("stdout", "code"),
    [
        pytest.param("", 1, id="runsc-refused"),
        pytest.param("not json", 0, id="garbage"),
        pytest.param(json.dumps({"data": {}}), 0, id="no-memory-section"),
    ],
)
def test_a_reading_runsc_does_not_give_is_none(stdout: str, code: int) -> None:
    assert runsc_usage_bytes("chat-a", run=_run(stdout, code)) is None
