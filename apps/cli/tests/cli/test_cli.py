"""Smoke tests for the Alkera CLI."""

from __future__ import annotations

import httpx
from alkera_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


def _ok_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/health/live":
        return httpx.Response(200, json={"status": "ok"})
    if request.url.path == "/health/ready":
        return httpx.Response(200, json={"status": "ok", "checks": {"db": "ok"}})
    return httpx.Response(404, json={"detail": "not found"})


def _down_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def test_version_command():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "alkera-cli" in result.stdout


def test_health_command_all_ok(monkeypatch):
    transport = httpx.MockTransport(_ok_handler)

    from alkera_cli import main as cli_main

    real_client = cli_main.AlkeraClient

    def patched_client(*args, **kwargs):
        kwargs["httpx_args"] = {"transport": transport}
        return real_client(*args, **kwargs)

    monkeypatch.setattr(cli_main, "AlkeraClient", patched_client)
    result = runner.invoke(app, ["health", "--url", "http://localhost:8000"])
    assert result.exit_code == 0, result.stdout
    assert "health/live" in result.stdout
    assert "health/ready" in result.stdout


def test_health_command_api_down(monkeypatch):
    transport = httpx.MockTransport(_down_handler)

    from alkera_cli import main as cli_main

    real_client = cli_main.AlkeraClient

    def patched_client(*args, **kwargs):
        kwargs["httpx_args"] = {"transport": transport}
        return real_client(*args, **kwargs)

    monkeypatch.setattr(cli_main, "AlkeraClient", patched_client)
    result = runner.invoke(app, ["health", "--url", "http://localhost:8000"])
    assert result.exit_code == 1
    assert "down" in result.stdout
