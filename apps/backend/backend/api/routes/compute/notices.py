"""A provider's shutdown notice drains the machine it names.

``POST /api/v1/compute/shutdown-notices`` takes a
:class:`~alkera_core.compute.shutdown_notice.ShutdownNotice` from anything
holding the deployment's notice secret — an EventBridge rule for EC2's
scheduled events, or the email hook in ``alkera_core.compute.shutdown_notice``
for a provider that only writes email — and drains the machine through the
same :func:`backend.services.compute.provisioning.drain` an admin's click
runs, so the box takes no new chats and hands the ones it holds on while it
still can.

PUBLIC and unlisted, like the Stripe webhook: there is no session, and the
only trust is the HMAC signature (:func:`~alkera_core.compute.shutdown_notice.verify`).
With no secret configured the route answers 404 — a drain it cannot verify is
refused, never accepted. A failed signature is charged to its source's budget.
"""

from __future__ import annotations

from typing import Any

from alkera_core.compute.shutdown_notice import (
    SIGNATURE_HEADER,
    ShutdownNotice,
    ShutdownNoticeResult,
    verify,
)
from alkera_core.config import settings
from alkera_core.logging import get_logger
from alkera_core.models.compute import (
    COMPUTE_TERMINAL_STATES,
    DRAINING,
    ComputeAllocation,
    ComputeMachineType,
)
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy import select

from backend.api.rate_limit import charge_webhook_rejection
from backend.auth.dependencies import DbSession
from backend.services.compute import provisioning

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/compute", tags=["compute"], include_in_schema=False)

_PROVIDER = "compute-notice"


def _actor(notice: ShutdownNotice) -> dict[str, Any]:
    return {"kind": "system", "email": None, "name": f"shutdown notice ({notice.source})"}


@router.post("/shutdown-notices", response_model=ShutdownNoticeResult)
async def shutdown_notice(request: Request, db: DbSession) -> ShutdownNoticeResult:
    """Drain the machine a signed notice names. ``404`` when notices are off
    or the machine is not one this plane runs; ``401`` for a bad signature;
    ``409`` for a machine not serving (still booting, asleep); ``200`` with
    ``drained=false`` for one already draining."""
    secret = settings.compute_shutdown_notice_secret
    if secret is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    body = await request.body()
    if not verify(secret.get_secret_value(), body, request.headers.get(SIGNATURE_HEADER, "")):
        log.warning("compute.shutdown_notice.rejected")
        refusal = charge_webhook_rejection(request, provider=_PROVIDER)
        if refusal is not None:
            raise refusal.exception()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bad signature")
    try:
        notice = ShutdownNotice.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Malformed notice"
        ) from exc

    alloc = (
        await db.execute(
            select(ComputeAllocation)
            .join(ComputeMachineType, ComputeMachineType.id == ComputeAllocation.machine_type_id)
            .where(
                ComputeMachineType.provider == notice.provider,
                ComputeAllocation.provider_machine_id == notice.machine_id,
                ComputeAllocation.lifecycle == "workspace",
                ComputeAllocation.state.not_in(tuple(COMPUTE_TERMINAL_STATES)),
            )
            .order_by(ComputeAllocation.created_at.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if alloc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "machine_not_found", "message": "No live machine has that id"},
        )
    if alloc.state == DRAINING:
        already = ShutdownNoticeResult(allocation_id=str(alloc.id), state=DRAINING, drained=False)
        await db.rollback()
        return already
    reason = f"shutdown notice: {notice.reason}" if notice.reason else "shutdown notice"
    try:
        await provisioning.drain(
            db,
            alloc,
            actor=_actor(notice),
            reason=reason[:512],
            auto_terminate=notice.auto_terminate,
        )
    except provisioning.ProvisionError as exc:
        raise HTTPException(
            status_code=exc.status, detail={"code": exc.code, "message": exc.message}
        ) from exc
    log.info(
        "compute.shutdown_notice.drained",
        allocation_id=str(alloc.id),
        provider=notice.provider,
        source=notice.source,
    )
    return ShutdownNoticeResult(allocation_id=str(alloc.id), state=alloc.state, drained=True)
