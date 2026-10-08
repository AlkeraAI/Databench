"""Core daemon methods: ping, info, health, shutdown.

These exist for plumbing — the extension uses `ping` and `health` to
verify the daemon is reachable and alive, `info` to surface the version,
`shutdown` to ask for a graceful exit.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Literal

from alkera_cli.daemon.protocol import _DaemonModel, method

if TYPE_CHECKING:
    from alkera_cli.daemon.server import JsonRpcServer


# --- ping ------------------------------------------------------------------


class PingRequest(_DaemonModel):
    pass


class PingResponse(_DaemonModel):
    pong: Literal["pong"]
    daemon_version: str
    daemon_pid: int
    uptime_seconds: float


@method("ping")
async def ping(server: JsonRpcServer, params: PingRequest) -> PingResponse:
    return PingResponse(
        pong="pong",
        daemon_version=server.version,
        daemon_pid=os.getpid(),
        uptime_seconds=server.uptime_seconds,
    )


# --- info ------------------------------------------------------------------


class InfoRequest(_DaemonModel):
    pass


class InfoResponse(_DaemonModel):
    """Build + runtime introspection. Distinct from `ping` because it
    carries machine-relevant detail (platform, pid, log path) that the
    extension surfaces in its status bar tooltip / debug output.
    """

    daemon_version: str
    daemon_pid: int
    platform: str
    python_version: str
    log_file: str
    working_dir: str


@method("info")
async def info(server: JsonRpcServer, params: InfoRequest) -> InfoResponse:
    import platform
    import sys

    from alkera_cli.daemon.paths import daemon_log_file

    return InfoResponse(
        daemon_version=server.version,
        daemon_pid=os.getpid(),
        platform=f"{platform.system().lower()}-{platform.machine().lower()}",
        python_version=sys.version.split()[0],
        log_file=str(daemon_log_file()),
        working_dir=str(server.working_dir),
    )


# --- health (used by the extension's 15s heartbeat) ------------------------


class HealthRequest(_DaemonModel):
    pass


class HealthResponse(_DaemonModel):
    """Lightweight liveness probe.

    Distinct from ``ping`` so the heartbeat loop's call pattern is visible
    in logs and we can later add cheap counters (queue depth, in-flight
    handlers) without overloading ``ping``.
    """

    status: Literal["ok"]
    uptime_seconds: float
    inflight_requests: int


@method("health")
async def health(server: JsonRpcServer, params: HealthRequest) -> HealthResponse:
    return HealthResponse(
        status="ok",
        uptime_seconds=server.uptime_seconds,
        inflight_requests=len(server._inflight),
    )


# --- shutdown --------------------------------------------------------------


class ShutdownRequest(_DaemonModel):
    pass


class ShutdownResponse(_DaemonModel):
    message: Literal["shutting down"]


@method("shutdown")
async def shutdown(server: JsonRpcServer, params: ShutdownRequest) -> ShutdownResponse:
    """Ask the daemon to stop after responding.

    Behavior: we respond OK first, then flip the shutdown flag so the
    main loop exits cleanly. Stdin EOF achieves the same thing for free
    when the parent closes the pipe; this method exists so the extension
    can request shutdown without closing stdin (useful in tests).
    """
    server.request_shutdown()
    return ShutdownResponse(message="shutting down")


__all__ = [
    "HealthRequest",
    "HealthResponse",
    "InfoRequest",
    "InfoResponse",
    "PingRequest",
    "PingResponse",
    "ShutdownRequest",
    "ShutdownResponse",
]
