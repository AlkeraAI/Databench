"""Org audit trail: admin read + CSV export, member agent-activity ingest.

Reads are scoped to the caller's own org by ``require_org_admin``. The one
write route is member-level: every member's daemon reports its own sessions,
while reading stays admin-only.

The audit log is an Enterprise feature on Alkera's SaaS (self-hosted installs
pass through), and that plan gate belongs to the three ADMIN READ surfaces
alone. It is a plain dependency there: there is nothing to record about a page
the SPA already renders a Contact-Sales panel over.

The member WRITE is NOT plan-gated. Evidence is recorded for every
organization — as it always has been for every action the server itself takes —
and what the plan buys is the ability to read it. A gate on the write threw
away evidence nobody can get back, and a refused batch makes a daemon treat its
credential as dead, so every box in a plan-less org logged a 403 for ever and
dropped its spool with it. What the write does decide, through
``backend.authz.enforce``, is a fact nothing on this route ever checked: that
the caller is still a member of the organization the events are filed under.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from alkera_core.authz import Action, Resource, ResourceType, Role
from alkera_core.models import OrgAuditEvent
from alkera_core.schemas.system.org_audit import (
    AgentAuditAccepted,
    AgentAuditBatch,
    AgentAuditEventIn,
    AuditChainVerification,
    OrgAuditEventRead,
    OrgAuditPage,
)
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from backend.api.rate_limit import limited
from backend.auth.dependencies import (
    CurrentOrg,
    CurrentPrincipal,
    CurrentUser,
    DbSession,
    OrgAdmin,
    require_enterprise_features,
)
from backend.authz import enforce, role_resolver
from backend.services.audit import org_audit as org_audit_service

router = APIRouter(prefix="/api/v1/org/audit-events", tags=["org-audit"])

#: The code the read refusal carries. The shared Enterprise dependency answers a
#: bare sentence, which every surface it guards shows the same way; the audit
#: trail has readers that are not a browser — an export script, a compliance
#: job — and they need to tell "this org has not bought the trail" from every
#: other 403 without matching on English.
AUDIT_NOT_ENTITLED = "audit_not_entitled"


async def require_audit_read_plan(request: Request, user: CurrentUser, db: DbSession) -> None:
    """The Enterprise plan gate on the READ surfaces, under its own code.

    Same verdict as :func:`require_enterprise_features` — it is the one source
    of truth for the plan, shared with the SSO surfaces and the SPA's own
    signal — restated with the code above so a refusal is machine-readable.
    Never applied to the ingest: see the module docstring.
    """
    try:
        await require_enterprise_features(request, user, db)
    except HTTPException as refusal:
        if refusal.status_code != status.HTTP_403_FORBIDDEN:
            raise
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": AUDIT_NOT_ENTITLED, "message": str(refusal.detail)},
        ) from refusal


_ENTERPRISE = Depends(require_audit_read_plan)

# One event's detail, serialized. Audit rows are who/what/when/outcome; anything
# bigger than this is content that belongs in the local trace.
_DETAIL_CAP_BYTES = 4096


#: The ingest is the one member-writable door into an admin-read audit chain, and
#: the schema bounds a batch but nothing bounded how many batches. Unbounded, a
#: member can bury a genuine denial entry under noise the admin's view, export
#: and chain verification all have to scan, and grow a shared table doing it.
#: Each member's daemon flushes a batch every few seconds when there is activity,
#: so a busy machine — and an office sharing one egress address — still sits well
#: under this. Sized generously on purpose: a refused batch is spooled and
#: retried by the client, but a trail that keeps bouncing off a throttle is an
#: audit gap, which is the worse failure of the two.
_INGEST_THROTTLE = limited("machine")


def _detail_bytes(event: AgentAuditEventIn) -> int:
    return len(json.dumps(event.detail, separators=(",", ":"), default=str).encode("utf-8"))


def _detail_too_large(index: int) -> HTTPException:
    message = f"events[{index}].detail exceeds {_DETAIL_CAP_BYTES} bytes"
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=message)


def _utc(bound: datetime | None) -> datetime | None:
    """A bound with no offset reads as UTC — matching the stored timestamps —
    instead of whatever the DB session's timezone happens to be."""
    if bound is None or bound.tzinfo is not None:
        return bound
    return bound.replace(tzinfo=UTC)


async def _filters(
    action: str | None = Query(default=None, max_length=200, description="Action prefix match"),
    actor_email: str | None = Query(default=None, max_length=320),
    created_after: datetime | None = Query(
        default=None, description="Inclusive lower bound; no offset reads as UTC"
    ),
    created_before: datetime | None = Query(
        default=None, description="Inclusive upper bound; no offset reads as UTC"
    ),
) -> org_audit_service.AuditFilters:
    return org_audit_service.AuditFilters(
        action=action,
        actor_email=actor_email,
        created_after=_utc(created_after),
        created_before=_utc(created_before),
    )


Filters = Annotated[org_audit_service.AuditFilters, Depends(_filters)]


#: Cell prefixes a spreadsheet evaluates instead of displaying. `target` is
#: member-supplied (the agent ingest below is deliberately member-level), and the
#: export is exactly the artifact an org admin opens in Excel.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value: str) -> str:
    """Neutralize a formula-leading cell by prefixing an apostrophe (the OWASP
    fix — spreadsheets keep showing the original text).

    Done at EXPORT, never at ingest: the stored value feeds the tamper-evident
    audit hash chain and must stay byte-faithful to what the client sent.
    """
    return "'" + value if value.startswith(_FORMULA_LEAD) else value


def _read(event: OrgAuditEvent) -> OrgAuditEventRead:
    return OrgAuditEventRead(
        id=event.id,
        actor_email=event.actor_email,
        action=event.action,
        target=event.target,
        detail=event.detail,
        created_at=event.created_at,
    )


@router.post(
    "/agent",
    response_model=AgentAuditAccepted,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_INGEST_THROTTLE)],
)
async def ingest_agent_events(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    ctx: CurrentPrincipal,
    batch: AgentAuditBatch,
) -> AgentAuditAccepted:
    """Ingest a daemon-reported batch of agent-activity events.

    The actor comes from the caller's token, never the payload. Actions are
    limited to the ``agent.*`` vocabulary at the schema layer. The org is the
    caller's credential's."""
    org_id = ctx.org_id
    # Roles at the org ROOT and nowhere else: a member row somewhere below is
    # not standing to write the organization's own trail, and descent only ever
    # runs the other way.
    roles = await role_resolver(request, db, ctx).for_team(org_id)
    await enforce(
        request,
        db,
        ctx,
        Action.WRITE,
        Resource(ResourceType.ORG_AUDIT, id=str(org_id), org_id=org_id),
        {
            "is_org_root": org_id == ctx.org_id,
            "org_member": roles.in_org and Role.MEMBER in roles.roles,
        },
    )
    # The schema pins the agent vocabulary as a closed set; this is the guard
    # that survives a future widening of it. Authorization decisions in
    # particular are server-only: a client naming one is forging the record.
    if any(not org_audit_service.client_reportable(e.action) for e in batch.events):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="only agent.* actions may be reported by a client",
        )
    oversized = [i for i, e in enumerate(batch.events) if _detail_bytes(e) > _DETAIL_CAP_BYTES]
    if oversized:
        raise _detail_too_large(oversized[0])
    accepted = await org_audit_service.record_agent_batch(
        db, org_id=org_id, actor=user, events=batch.events
    )
    return AgentAuditAccepted(accepted=accepted)


@router.get("", response_model=OrgAuditPage, dependencies=[_ENTERPRISE])
async def list_events(
    db: DbSession,
    _admin: OrgAdmin,
    org_id: CurrentOrg,
    filters: Filters,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> OrgAuditPage:
    events, total = await org_audit_service.list_page(
        db, org_id=org_id, offset=offset, limit=limit, filters=filters
    )
    return OrgAuditPage(events=[_read(e) for e in events], total=total, offset=offset, limit=limit)


@router.get("/verify", response_model=AuditChainVerification, dependencies=[_ENTERPRISE])
async def verify_chain(
    db: DbSession, _admin: OrgAdmin, org_id: CurrentOrg
) -> AuditChainVerification:
    """Verify the org's tamper-evident audit hash chain — detects any edited,
    deleted, re-ordered, or inserted event."""
    result = await org_audit_service.verify_chain(db, org_id=org_id)
    return AuditChainVerification(
        ok=result.ok,
        checked=result.checked,
        broken_event_id=result.broken_event_id,
        broken_at=result.broken_at,
        head_hash=result.head_hash,
    )


@router.get("/export.csv", dependencies=[_ENTERPRISE])
async def export_csv(
    db: DbSession, _admin: OrgAdmin, org_id: CurrentOrg, filters: Filters
) -> StreamingResponse:
    """CSV of the org's events, narrowed by the same filters as the list.

    The whole trail, however long it is. An audit export is what an admin
    hands a regulator, so a cap on it is a silently incomplete answer to the
    one question where that is worst — and building the document in memory to
    apply the cap is what made the cap look necessary. The rows are read in
    batches on a seek and each one is written into the same small buffer and
    sent, so the export costs one batch of memory at any length.
    """

    async def document() -> AsyncIterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["timestamp", "actor_email", "action", "target", "detail"])
        async for e in org_audit_service.iter_all(db, org_id=org_id, filters=filters):
            writer.writerow(
                [
                    e.created_at.isoformat(),
                    _csv_cell(e.actor_email),
                    _csv_cell(e.action),
                    _csv_cell(e.target or ""),
                    _csv_cell(json.dumps(e.detail)) if e.detail is not None else "",
                ]
            )
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
        rest = buffer.getvalue()
        if rest:
            yield rest

    return StreamingResponse(
        document(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; filename=org-audit-events.csv",
            "X-Content-Type-Options": "nosniff",
        },
    )
