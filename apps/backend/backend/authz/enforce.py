"""enforce(): decide through authorize(), answer the HTTP status, leave the
decision on record.

The pure engine (``alkera_core.authz.authorize``) knows nothing of HTTP or the
database. This module is the one place that connects it to both: it turns a
:class:`~alkera_core.authz.Decision` into the response the route has always
given — 403 with the policy's message (structured when the policy names an
error code), or an opaque 404 when the denial must not confirm the resource
exists — and writes an ``authz.decision`` row to the event outbox for EVERY
decision, allow and deny alike.

Where the row is written matters more than that it is written:

* an ALLOW goes into the request's own session, so it commits with the
  mutation it authorised and disappears with it if the request later rolls
  back — an allow row never claims a write that did not land;
* a DENY goes into a dedicated session that commits at once, because the
  request that produced it is about to fail and roll back, and a denial that
  vanished with the rollback would be the one record nobody could audit.

Recording a denial never touches the caller's session. Its session comes from
a small pool of its own (``DecisionSessionLocal``), never the request pool, so
a refused request never asks the request pool for a second connection, and
the caller's transaction, its earlier writes and every row it has loaded are
exactly as they were: a listing that refuses one row and goes on to the next
reads the rows it already holds, and a batch keeps the items it already wrote.
A failure to write the deny row is logged at error level and never turns the
denial into a 500: the caller is refused either way, and an outbox outage must
not change what a refusal looks like. A failure to write an ALLOW row
propagates — an allow that cannot be recorded does not proceed.

Redaction, stated once: the row's payload carries only the attributes the
policy declared auditable, coerced to short scalars by ``audited_attrs``;
``register()`` already refuses any policy whose allowlist names a secret or a
body. The ``actor`` document is the acting context's chain (ids, org ids, and
labels of the same class the org audit trail already stores). Rows carry
``visibility="platform"``: no tenant stream receives them.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Protocol

from alkera_core.authz import (
    AUTHZ_EVENT_TYPE,
    BATCH_ENTITY_ID,
    ActingContext,
    Action,
    BatchDecisionEvent,
    Decision,
    DecisionEvent,
    Resource,
    RoleResolver,
    audited_attrs,
    authorize,
    policy_for,
)
from alkera_core.db.session import DecisionSessionLocal
from alkera_core.events import VISIBILITY_PLATFORM, emit
from alkera_core.logging import get_logger
from alkera_core.observability import metrics
from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.org import teams as team_service

log = get_logger(__name__)


class DecisionSink(Protocol):
    """Where decisions are recorded. The outbox in production; a recording
    sink in tests that only care about the decision, not the row."""

    async def record_allow(self, db: AsyncSession, event: DecisionEvent) -> None: ...

    async def record_deny(self, db: AsyncSession, event: DecisionEvent) -> None: ...

    async def record_batch(self, db: AsyncSession, event: BatchDecisionEvent) -> None: ...


async def _emit(session: AsyncSession, event: DecisionEvent | BatchDecisionEvent) -> None:
    if isinstance(event, BatchDecisionEvent):
        entity, entity_id = event.resource_type, BATCH_ENTITY_ID
    else:
        entity, entity_id = event.resource.type.value, event.recorded_id
    await emit(
        session,
        org_id=event.org_id,
        type=AUTHZ_EVENT_TYPE,
        entity=entity,
        entity_id=entity_id,
        version=0,
        actor=event.actor,
        visibility=VISIBILITY_PLATFORM,
        payload=event.outbox_payload(),
    )


async def _emit_on_its_own(event: DecisionEvent | BatchDecisionEvent) -> None:
    """Commit ``event`` in a session from the decision pool; log a failure."""
    try:
        async with DecisionSessionLocal() as session:
            await _emit(session, event)
            await session.commit()
    except Exception:  # the answer must reach the caller whatever the outbox does
        if isinstance(event, BatchDecisionEvent):
            kind, resource_type, reason = "batch", event.resource_type, "batch"
        else:
            kind, resource_type, reason = "deny", event.resource.type.value, event.decision.reason
        metrics.record_authz_decision_write_failed(kind)
        log.error(
            "authz.decision.write_failed",
            action=event.action.value,
            resource_type=resource_type,
            reason=reason,
            exc_info=True,
        )


class OutboxDecisionSink:
    """The production sink: allows in the request session, denies in a
    session of their own from the decision pool (see the module docstring)."""

    async def record_allow(self, db: AsyncSession, event: DecisionEvent) -> None:
        await _emit(db, event)

    async def record_deny(self, db: AsyncSession, event: DecisionEvent) -> None:
        await _emit_on_its_own(event)

    async def record_batch(self, db: AsyncSession, event: BatchDecisionEvent) -> None:
        # A batch summary is the record of a read, with no mutation for it to
        # ride, and it may carry refusals: it is written the way a denial is.
        await _emit_on_its_own(event)


class SettledRepeatSink:
    """Wraps a sink so an unchanged ALLOW on a liveness beat is recorded once
    per window instead of once per beat.

    A box beats every fifteen seconds for as long as it lives. The decision
    behind each beat is worth keeping -- it is what proves the box was still
    entitled to serve -- but the second, third and thousandth identical copy of
    it prove nothing the first does not, and at four rows a minute per box the
    outbox fills with them forever. So the first ALLOW for a caller is recorded
    and the repeats of it are not, until the window lapses or the decision
    itself changes.

    Two properties make the suppression safe to reason about:

    * a DENY is ALWAYS recorded, and clears the remembered allow -- the beat
      after a revoke gets its own row, and so does the next allow after that,
      so a revoke-and-restore is never hidden inside a suppressed run;
    * the fingerprint covers the whole decision (effect, reason, policy and
      every audited attribute), so a beat whose facts changed is a different
      decision and is recorded, not folded into the run before it.

    The memory is per-process, which is the deliberate limit: with N backend
    processes a box writes at most N rows per window rather than one, still
    orders of magnitude below one per beat, and no coordination or table is
    needed to get it. Use it only for a repeated liveness call; every other
    action keeps one row per decision.
    """

    def __init__(self, inner: DecisionSink, *, window: timedelta) -> None:
        self._inner = inner
        self._window = window
        self._seen: dict[tuple[str, ...], tuple[tuple[object, ...], datetime]] = {}

    @staticmethod
    def _key(event: DecisionEvent) -> tuple[str, ...]:
        # Who is beating about what: the acting chain, the resource it names,
        # and the route it called. A second box, or the same box on another
        # resource, keeps its own run.
        actor = event.actor
        return (
            str(event.org_id),
            str(actor.get("acting_principal")),
            str(actor.get("delegating_user")),
            event.resource.type.value,
            event.resource.id or "",
            event.path,
        )

    @staticmethod
    def _fingerprint(event: DecisionEvent) -> tuple[object, ...]:
        d = event.decision
        return (
            event.action.value,
            d.effect.value,
            d.reason,
            d.policy,
            tuple(sorted((k, repr(v)) for k, v in event.attrs.items())),
        )

    def _prune(self, now: datetime) -> None:
        stale = [k for k, (_, at) in self._seen.items() if now - at >= self._window]
        for k in stale:
            del self._seen[k]

    def clear(self) -> None:
        """Forget every remembered decision. For tests, and for a process that
        wants the next beat recorded whatever it decided last."""
        self._seen.clear()

    async def record_allow(self, db: AsyncSession, event: DecisionEvent) -> None:
        now = datetime.now(UTC)
        key = self._key(event)
        fingerprint = self._fingerprint(event)
        seen = self._seen.get(key)
        if seen is not None and seen[0] == fingerprint and now - seen[1] < self._window:
            return
        self._prune(now)
        self._seen[key] = (fingerprint, now)
        await self._inner.record_allow(db, event)

    async def record_deny(self, db: AsyncSession, event: DecisionEvent) -> None:
        # A denial is never suppressed, and it ends the allowed run: whatever
        # comes back after it is a change worth a row of its own.
        self._seen.pop(self._key(event), None)
        await self._inner.record_deny(db, event)

    async def record_batch(self, db: AsyncSession, event: BatchDecisionEvent) -> None:
        await self._inner.record_batch(db, event)


_default_sink: DecisionSink = OutboxDecisionSink()


def default_sink() -> DecisionSink:
    """The sink a decision is recorded in when the caller names none."""
    return _default_sink


def http_error_for(decision: Decision) -> HTTPException:
    """The response a denial has always produced on these routes.

    An opaque denial is a 404 carrying the policy's not-found text; a denial
    with an error code is a 403 whose detail is ``{"code", "message"}`` so a
    client can key off the code; every other denial is a 403 with the message
    as its detail. A denial that refuses the credential itself is a 401
    (``{"code", "message"}`` when it names a code), so a client stops
    presenting it instead of retrying.
    """
    if decision.unauthenticated:
        detail: object = (
            {"code": decision.error_code, "message": decision.message}
            if decision.error_code is not None
            else decision.message
        )
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )
    if decision.as_not_found:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=decision.message)
    if decision.error_code is not None:
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": decision.error_code, "message": decision.message},
        )
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=decision.message)


async def enforce(
    request: Request,
    db: AsyncSession,
    ctx: ActingContext,
    action: Action,
    resource: Resource,
    attrs: Mapping[str, object],
    *,
    sink: DecisionSink | None = None,
) -> Decision:
    """Decide, record, and either return the allow or raise the denial.

    ``attrs`` are the facts the route resolved; only the policy's declared
    subset reaches the row. The row is filed under the resource's org when the
    resource names one (a cross-org attempt is recorded against the org whose
    resource was probed), else the caller's.

    An allow row rides the transaction of the mutation it authorised; a
    denial is recorded without touching it (see :class:`OutboxDecisionSink`).
    """
    decision = await decide_on_record(
        db,
        ctx,
        action,
        resource,
        attrs,
        method=request.method,
        path=request.url.path,
        sink=sink,
    )
    if decision.allowed:
        return decision
    raise http_error_for(decision)


async def decide_on_record(
    db: AsyncSession,
    ctx: ActingContext,
    action: Action,
    resource: Resource,
    attrs: Mapping[str, object],
    *,
    method: str,
    path: str,
    sink: DecisionSink | None = None,
) -> Decision:
    """:func:`enforce` for a caller with no HTTP request (a message on a
    realtime socket): the same decision, recorded the same way (an allow in
    ``db``, a deny in a committed session of its own), but returned rather
    than raised, so the caller answers a refusal in its own protocol.
    ``method`` names what the caller is and ``path`` where it acts, in place
    of an HTTP verb and URL."""
    decision = authorize(ctx, action, resource, attrs)
    policy = policy_for(resource.type)
    event = DecisionEvent(
        org_id=resource.org_id or ctx.org_id,
        actor=ctx.audit_dict(),
        action=action,
        resource=resource,
        decision=decision,
        attrs=audited_attrs(attrs, policy.audited_attrs if policy else frozenset()),
        method=method,
        path=path,
    )
    chosen = sink or _default_sink
    if decision.allowed:
        await chosen.record_allow(db, event)
    else:
        await chosen.record_deny(db, event)
    return decision


def role_resolver(request: Request, db: AsyncSession, ctx: ActingContext) -> RoleResolver:
    """The request's role resolver, built once and bound to the real ancestor
    chain loader, so a route that asks about several teams walks each once."""
    cached: RoleResolver | None = getattr(request.state, "role_resolver", None)
    if cached is None:
        cached = RoleResolver(db, ctx, ancestor_chain=team_service.ancestor_chain)
        request.state.role_resolver = cached
    return cached


__all__ = [
    "DecisionSink",
    "OutboxDecisionSink",
    "SettledRepeatSink",
    "decide_on_record",
    "default_sink",
    "enforce",
    "http_error_for",
    "role_resolver",
]
