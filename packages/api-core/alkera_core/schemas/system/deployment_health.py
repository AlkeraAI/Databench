"""Deployment-health API shapes (the org-admin Health tab)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class DeploymentHealthCheckRead(BaseModel):
    key: str
    label: str
    status: Literal["ok", "warn", "fail", "skipped"]
    detail: str
    latency_ms: int
    # True when the row pertains to the caller's org (a provider probe) rather
    # than the instance as a whole.
    org_scoped: bool


class DeploymentHealthReport(BaseModel):
    # Worst non-skipped status across all checks — the banner signal.
    overall: Literal["ok", "warn", "fail"]
    checks: list[DeploymentHealthCheckRead]
    last_run_at: datetime | None
    last_trigger: Literal["scheduled", "manual"] | None
    last_duration_ms: int | None
    mode: Literal["proxy", "direct"]
    backend_version: str
