"""The container liveness probe: ``python -m worker health``.

Standard library only, on purpose. ECS runs this command in a fresh
interpreter every 30 s with a 5 s budget, on tasks as small as 0.5 vCPU that
are also running the worker being probed. Importing the worker runtime to make
one HTTP GET overran that budget in production, and ECS replaced healthy
tasks for it. Nothing here may import ``alkera_core``, ``temporalio``,
``sqlalchemy`` or ``worker.temporal``; ``test_main.py`` pins that.
"""

from __future__ import annotations

import argparse
import os
import urllib.error
import urllib.request

# Settings.alkera_worker_health_{host,port} without loading the settings
# module: the same environment names and the same defaults (a test keeps the
# two in step). The probe reads the process environment only, which is all a
# container has.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9000


def default_host() -> str:
    return os.environ.get("ALKERA_WORKER_HEALTH_HOST", DEFAULT_HOST)


def default_port() -> int:
    raw = os.environ.get("ALKERA_WORKER_HEALTH_PORT", "").strip()
    return int(raw) if raw.isdigit() else DEFAULT_PORT


def probe_health(host: str, port: int, *, timeout_s: float = 3.0) -> tuple[int, str]:
    """``(status code, body)`` of one liveness probe; a refused connection or a
    timeout is reported as status 0."""
    url = f"http://{host}:{port}/health/live"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:  # noqa: S310 - fixed http://host:port we built
            return int(response.status), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return 0, str(exc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m worker health",
        description="probe this container's /health/live; exit 0 or 1",
    )
    parser.add_argument("--host", default=default_host())
    parser.add_argument("--port", type=int, default=default_port())
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    code, body = probe_health(args.host, args.port)
    print(body)
    return 0 if code == 200 else 1
