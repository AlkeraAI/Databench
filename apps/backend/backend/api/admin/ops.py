"""Admin: SaaS-wide health on one page — machines by state and provider,
chats served, gateway spend against the platform cap, crash reports,
deployment health and the running build.

A read at the router's platform-staff floor; the page polls it every 30 s.
"""

from __future__ import annotations

from alkera_core.schemas.system.ops import OpsSummary
from fastapi import APIRouter

from backend.api.admin._audit import AuditedRoute
from backend.auth.dependencies import DbSession
from backend.services.ops import ops_summary as ops_service

router = APIRouter(prefix="/ops", route_class=AuditedRoute)


@router.get("/summary", response_model=OpsSummary)
async def ops_summary(db: DbSession) -> OpsSummary:
    return await ops_service.summary(db)
