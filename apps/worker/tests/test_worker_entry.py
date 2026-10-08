"""The worker's dispatch (``worker.entry``): the open entry and the product's
both go through it, the product passing the install of its extensions."""

from __future__ import annotations

import sys
import types

import pytest
from worker import entry

pytestmark = [pytest.mark.spread]


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    cli = types.ModuleType("worker.cli")
    setattr(cli, "main", lambda argv: seen.append(f"cli {argv}") or 0)  # noqa: B010
    probe = types.ModuleType("worker.health_probe")
    setattr(probe, "main", lambda argv: seen.append(f"probe {argv}") or 0)  # noqa: B010
    monkeypatch.setitem(sys.modules, "worker.cli", cli)
    monkeypatch.setitem(sys.modules, "worker.health_probe", probe)
    return seen


def test_a_command_runs_after_the_install(calls: list[str]) -> None:
    assert entry.main(["run"], lambda: calls.append("install")) == 0
    assert calls == ["install", "cli ['run']"]


def test_the_open_entry_installs_nothing(calls: list[str]) -> None:
    assert entry.main(["run"]) == 0
    assert calls == ["cli ['run']"]


def test_the_health_probe_never_installs(calls: list[str]) -> None:
    assert entry.main(["health", "--port", "9"], lambda: calls.append("install")) == 0
    assert calls == ["probe ['--port', '9']"]
