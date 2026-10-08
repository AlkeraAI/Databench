"""Shared by the production runtime build characterizations: a recorder for
every ``HarnessRuntime`` construction and the probes the tests read it with."""

from __future__ import annotations

import types
from typing import Any

import pytest
from alkera_cli.contracts.gateway_model import GatewayModel
from alkera_cli.harness.claude_gateway import default_claude_env_builder
from alkera_cli.harness.runtime import HarnessRuntime
from alkera_cli.observability.audit_report import default_reporter
from alkera_cli.observability.otel_export import default_exporter

CATALOG = [
    GatewayModel(id="m-one", display_name="One", wire="anthropic", family="claude"),
    GatewayModel(id="m-two", display_name="Two", wire="openai", family="gpt"),
]


def record_builds(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, dict[str, Any]]]:
    """Every ``HarnessRuntime`` construction, as ``(project, kwargs)``. The real
    constructor never runs: the build's own arguments are the subject."""
    calls: list[tuple[Any, dict[str, Any]]] = []

    def record(self: HarnessRuntime, project: Any, **kwargs: Any) -> None:
        calls.append((project, kwargs))
        self._project = project

    monkeypatch.setattr(HarnessRuntime, "__init__", record)
    return calls


def closure_values(fn: types.FunctionType) -> list[Any]:
    return [cell.cell_contents for cell in fn.__closure__ or ()]


def fetch_qualname(resolver: Any) -> str:
    """The catalog lister a tier resolver reads with (the default vs the machine one)."""
    return str(resolver._fetch.__qualname__)


def assert_shared_tail(kwargs: dict[str, Any]) -> None:
    assert kwargs["claude_env_builder"] is default_claude_env_builder
    assert kwargs["audit_reporter"] is default_reporter()
    assert kwargs["otel_exporter"] is default_exporter()
