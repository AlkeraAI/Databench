"""Which probe each orchestrator acts on: restarts follow /health/live, traffic
follows /health/ready.

/health/ready answers 200 with ``status: "degraded"`` inside its grace window
and 503 past it, because the database it asks is shared by every container. A
restart cannot fix a shared database, so nothing that restarts a container may
watch it: compose acts on an unhealthy container (a restart policy, a
dependant held back), so its healthchecks probe liveness; the chart's
readinessProbe only takes a pod out of the Service, so it keeps readiness.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.spread]

REPO = Path(__file__).resolve().parents[3]
COMPOSE = REPO / "deploy" / "docker" / "compose.prod.example.yml"
APP_TEMPLATE = REPO / "deploy" / "helm" / "alkera" / "templates" / "app.yaml"

#: The services that answer both probes, and the port each listens on.
SERVED = {"backend": 8000, "gateway": 8081}


def compose_probe_paths(text: str) -> dict[str, list[str]]:
    """Every health path each served service's compose healthcheck names."""
    services: dict[str, Any] = yaml.safe_load(text)["services"]
    found: dict[str, list[str]] = {}
    for name in SERVED:
        test = " ".join(services[name]["healthcheck"]["test"])
        found[name] = re.findall(r"/health/\w+", test)
    return found


def helm_probe_paths(text: str) -> dict[tuple[int, str], str]:
    """``(port, probe kind) -> path`` for each served port's probes."""
    found: dict[tuple[int, str], str] = {}
    pattern = re.compile(
        r"(readinessProbe|livenessProbe|startupProbe):\s*\n\s*httpGet:\s*\{\s*path:\s*"
        r"(/health/\w+),\s*port:\s*(\d+)"
    )
    for kind, path, port in pattern.findall(text):
        if int(port) in SERVED.values():
            found[(int(port), kind)] = path
    return found


def test_compose_restarts_on_liveness_only() -> None:
    assert compose_probe_paths(COMPOSE.read_text(encoding="utf-8")) == {
        "backend": ["/health/live"],
        "gateway": ["/health/live"],
    }


def test_the_chart_routes_traffic_on_readiness_and_restarts_on_liveness() -> None:
    paths = helm_probe_paths(APP_TEMPLATE.read_text(encoding="utf-8"))
    for port in SERVED.values():
        assert paths[(port, "readinessProbe")] == "/health/ready", port
        assert paths[(port, "livenessProbe")] == "/health/live", port
        assert paths[(port, "startupProbe")] == "/health/live", port


def test_the_compose_read_catches_a_healthcheck_on_readiness() -> None:
    planted = (
        "services:\n"
        "  backend:\n"
        "    healthcheck:\n"
        "      test: [CMD, curl, 'http://localhost:8000/health/ready']\n"
        "  gateway:\n"
        "    healthcheck:\n"
        "      test: [CMD, curl, 'http://localhost:8081/health/live']\n"
    )
    assert compose_probe_paths(planted) == {
        "backend": ["/health/ready"],
        "gateway": ["/health/live"],
    }


def test_the_chart_read_catches_a_liveness_probe_on_readiness() -> None:
    planted = (
        "          livenessProbe:\n"
        "            httpGet: { path: /health/ready, port: 8000 }\n"
        "          readinessProbe:\n"
        "            httpGet: { path: /health/ready, port: 9000 }\n"
    )
    assert helm_probe_paths(planted) == {(8000, "livenessProbe"): "/health/ready"}
