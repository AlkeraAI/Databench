"""Org-admin deployment health (/api/v1/org/deployment-health).

Self-hosted-only (a bare 404 on SaaS — the page does not exist there). The GET
returns the latest snapshot, overlaying the schedule-liveness self-check at read time
so a dead worker turns red without waiting for a run; the POST runs the same
shared runner inline (instant feedback) and audits the manual run.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alkera_core.config import settings
from alkera_core.deployment_health import CheckResult as _CheckResult
from alkera_core.deployment_health import (
    RunMeta,
    load_snapshot,
    run_and_persist,
    worker_beat_result,
)
from alkera_core.schemas.system.deployment_health import (
    DeploymentHealthCheckRead,
    DeploymentHealthReport,
)
from fastapi import APIRouter, Depends, HTTPException, status

from backend.auth.dependencies import CurrentOrg, DbSession, OrgAdmin, OrgAdminVerified
from backend.services.audit import org_audit as org_audit_service
from backend.services.files import admin_store


async def require_self_hosted() -> None:
    """The Deployment page is self-hosted-only — a bare 404 on SaaS."""
    if not settings.is_self_hosted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


router = APIRouter(
    prefix="/api/v1/org/deployment-health",
    tags=["deployment-health"],
    dependencies=[Depends(require_self_hosted)],
)

# Instance-level checks a warn on shouldn't push the banner to fail; but a fail on
# any real check should. "overall" = worst non-skipped status.
_SEVERITY = {"ok": 0, "warn": 1, "fail": 2}


def _report(results: list[_CheckResult], meta: RunMeta | None) -> DeploymentHealthReport:
    # Overlay the schedule-liveness self-check at read time (a stale worker turns red
    # without needing a manual run).
    now = datetime.now(UTC)
    overlaid = [r for r in results if r.key != "worker_beat"]
    overlaid.append(worker_beat_result(meta.last_scheduled_at if meta else None, now))

    worst = max((_SEVERITY[r.status] for r in overlaid if r.status != "skipped"), default=0)
    overall = {0: "ok", 1: "warn", 2: "fail"}[worst]
    return DeploymentHealthReport(
        overall=overall,  # type: ignore[arg-type]
        checks=[
            DeploymentHealthCheckRead(
                key=r.key,
                label=r.label,
                status=r.status,
                detail=r.detail,
                latency_ms=r.latency_ms,
                org_scoped=r.org_team_id is not None,
            )
            for r in overlaid
        ],
        last_run_at=meta.last_run_at if meta else None,
        last_trigger=meta.last_trigger if meta else None,
        last_duration_ms=meta.last_duration_ms if meta else None,
        mode="proxy" if settings.gateway_proxy_mode else "direct",
        backend_version=settings.app_version,
    )


@router.get("", response_model=DeploymentHealthReport)
async def get_deployment_health(
    db: DbSession, admin: OrgAdmin, org_id: CurrentOrg
) -> DeploymentHealthReport:
    results, meta = await load_snapshot(db, org_team_id=org_id)
    return _report(results, meta)


@router.post("/run", response_model=DeploymentHealthReport)
async def run_deployment_health(
    db: DbSession, admin: OrgAdminVerified, org_id: CurrentOrg
) -> DeploymentHealthReport:
    """Run the checks NOW (inline — instant feedback; the same runner the scheduled
    run uses, so results are identical). Audited."""
    results = await run_and_persist(trigger="manual", files_admin_store=admin_store)
    _, meta = await load_snapshot(db, org_team_id=org_id)
    report = _report([r for r in results if r.org_team_id in (None, org_id)], meta)
    await org_audit_service.record(
        db,
        org_id=org_id,
        actor=admin,
        action="deployment_health.ran",
        detail={"overall": report.overall, "duration_ms": report.last_duration_ms},
    )
    return report
