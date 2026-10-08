"""A box posts its supervisor's events: ``POST /api/v1/machines/me/logs``.

For a box with no direct route into the deployment's log store (RunPod, a
personal box, a local developer box): an EC2 box writes to CloudWatch Logs on
its instance role instead. Only the machine credential may post, and only as
its own machine: the machine id each event is logged under is the one the
credential holds, never one the body names. A person, a worker credential or a
dead credential is refused through ``compute.machine_credential`` like every
other request on the machine's own standing, and the decision is on record.

``202`` with how many events were logged and how many dropped (outside the
allowlist, or past the machine's event budget). A batch past
:data:`~alkera_core.compute.box_logs.MAX_BATCH_EVENTS` is ``422``.
"""

from __future__ import annotations

from alkera_core.authz import Action
from alkera_core.schemas.box_logs import BoxLogBatch, BoxLogBatchRead
from fastapi import APIRouter, Request, status

from backend.api.deps.machine_standing import decide_machine_standing
from backend.auth.dependencies import CurrentPrincipal, DbSession, machine_own_standing
from backend.services.compute import ingest_box_logs

router = APIRouter(prefix="/api/v1/machines", tags=["machines"])


@router.post("/me/logs", response_model=BoxLogBatchRead, status_code=status.HTTP_202_ACCEPTED)
@machine_own_standing
async def post_box_logs(
    request: Request, body: BoxLogBatch, db: DbSession, ctx: CurrentPrincipal
) -> BoxLogBatchRead:
    """Log the box's events as its own machine's."""
    machine_id = await decide_machine_standing(
        request,
        db,
        ctx,
        action=Action.WRITE,
        resource_id="logs",
        org_id=ctx.org_id,
        org_reached=True,
    )
    accepted, dropped = ingest_box_logs(str(machine_id), body)
    return BoxLogBatchRead(accepted=accepted, dropped=dropped)


__all__ = ["router"]
