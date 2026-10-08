"""Where the parent-hosted tool server listens: the daemon's own loopback,
except on a gVisor box, where a chat's container reaches the daemon at the host
end of its veth pair and the server must answer there too."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from alkera_cli.harness import sandbox as sb
from alkera_cli.harness.mcp_server import (
    AlkeraToolServer,
    SessionToolBinding,
    tool_server_bind_host,
)
from alkera_cli.plugins.plugin_base import ToolRegistry
from alkera_cli.plugins.plugin_base.permissions import DecisionSink
from alkera_core.project.directory import ProjectDirectory


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        pytest.param("gvisor", "0.0.0.0", id="gvisor-every-address"),
        pytest.param("none", "127.0.0.1", id="none-loopback"),
    ],
)
def test_the_bind_host_follows_the_box_mode(mode: sb.SandboxMode, expected: str) -> None:
    assert tool_server_bind_host(sb.SandboxSettings(mode=mode)) == expected


def test_the_default_is_the_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(sb.ENV_MODE, raising=False)
    assert tool_server_bind_host() == "127.0.0.1"


def _binding(tmp_path: Path) -> SessionToolBinding:
    project = ProjectDirectory(tmp_path / ".alkera")
    registry = ToolRegistry(project.blobs(), decision_sink=DecisionSink(project.path))
    return SessionToolBinding(registry=registry, session_id="s-bind")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "bound"),
    [
        pytest.param("gvisor", "0.0.0.0", id="gvisor"),
        pytest.param("none", "127.0.0.1", id="none"),
    ],
)
async def test_the_server_really_binds_where_the_mode_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, bound: str
) -> None:
    """Read at start, from the box's environment: on a gVisor box the socket is
    open on every address (the box firewall and the bearer token stand in
    front of it); elsewhere it is the loopback and nothing else. The URL the
    daemon hands out names the loopback either way, which both binds serve."""
    monkeypatch.setenv(sb.ENV_MODE, mode)
    server = AlkeraToolServer(_binding(tmp_path))
    await server.start()
    try:
        assert server._server is not None
        sock = server._server.servers[0].sockets[0]
        assert sock.getsockname()[0] == bound
        port = sock.getsockname()[1]
        assert server.url == f"http://127.0.0.1:{port}/mcp"
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            pass
    finally:
        await server.stop()
