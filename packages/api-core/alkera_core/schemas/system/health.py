"""Pydantic schemas for the /health endpoints.

Defining these (rather than letting FastAPI infer from raw dicts) lets the
OpenAPI schema describe the response shapes precisely, which flows through
to the typed TS client (`@alkera/sdk`) consumed by the frontend.
"""

from __future__ import annotations

from pydantic import BaseModel


class LiveStatus(BaseModel):
    status: str


class ReadyStatus(BaseModel):
    status: str
    db: str | None = None
    detail: str | None = None
    # Whether THIS process considers BYOK active (self-hosted + direct + entitled).
    # Reported by every process so the deployment-health runner can confirm the
    # gateway, backend, and worker agree — a split view means ALKERA_ENTITLEMENTS
    # isn't set identically everywhere, which half-activates BYOK.
    byok: bool = False


class InfoResponse(BaseModel):
    """Build / deployment introspection — what's actually running.

    Used by ops to verify which image is live in a customer's VPC without
    needing credentials. Mirrors what tools like Sentry or GitHub Releases
    expect to see in a `/health` or `/version` endpoint.
    """

    app: str
    version: str
    build_id: str | None
    env: str
