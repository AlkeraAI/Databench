"""A Files decision made outside an HTTP request, on the record like a route's.

A route authorizes a node through its request-scoped helpers. A background
path -- the Slack relay reading a chart to post, the Slack receiver writing a
person's attachment into their chat -- has no request, but its decision is the
same one: the same policy, against today's grants, filed as an
``authz.decision`` row. This is that decision, for a caller that names what it
is (``method``) and where it acts (``path``) instead of an HTTP verb and URL.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from alkera_core.authz import (
    ActingContext,
    Action,
    Decision,
    DecisionEvent,
    Resource,
    audited_attrs,
    policy_for,
)
from alkera_core.authz import authorize as decide
from alkera_core.files import NodeId
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.authorize import Authorized, authorize
from alkera_core.files.authz.decider import AccessFacts
from alkera_core.files.errors import FilesError
from alkera_core.files.repo import FilesRepo
from sqlalchemy.ext.asyncio import AsyncSession

from backend.authz import OutboxDecisionSink
from backend.services.files.facts import background_facts


async def authorized_node(
    db: AsyncSession,
    repo: FilesRepo,
    ctx: ActingContext,
    node_id: NodeId,
    action: FilesAction,
    *,
    method: str,
    path: str,
    facts: AccessFacts | None = None,
) -> Authorized[Any] | None:
    """``node_id`` authorized for ``action`` as ``ctx``, or ``None``.

    ``None`` covers every refusal with the same silence a missing node gets:
    a background caller's only answer is to not do the thing, and telling "no
    such file" apart from "you may not" would be an oracle for whoever sees
    the result.

    The facts are resolved before the Files transaction opens, because they are
    platform reads and the Files role is granted nothing outside the Files
    tables; the decision rows are filed after it closes, for the same reason.
    The sink keeps the caller's transaction: a refusal here is not about to
    roll anything back, and ending the transaction would throw away work the
    caller already did.

    ``facts`` are the caller's own when the server built the caller itself
    (a machine acting under the lease it holds, whose facts no person's
    roles resolve); resolved for ``ctx`` otherwise.
    """
    events: list[DecisionEvent] = []

    def record(
        ctx: ActingContext, action: Action, resource: Resource, attrs: Mapping[str, object]
    ) -> Decision:
        decision = decide(ctx, action, resource, attrs)
        policy = policy_for(resource.type)
        events.append(
            DecisionEvent(
                org_id=resource.org_id or ctx.org_id,
                actor=ctx.audit_dict(),
                action=action,
                resource=resource,
                decision=decision,
                attrs=audited_attrs(attrs, policy.audited_attrs if policy else frozenset()),
                method=method,
                path=path,
            )
        )
        return decision

    if facts is None:
        facts = await background_facts(db, ctx)
    allowed: Authorized[Any] | None = None
    async with repo.transaction():
        try:
            allowed = await authorize(ctx, repo, node_id, action, facts=facts, enforce=record)
        except FilesError:
            # Caught rather than allowed to leave the block so the transaction
            # ends in a COMMIT: a rollback would discard the caller's own work.
            allowed = None
    await _file_decisions(db, events)
    if allowed is None or allowed.node.trashed_at is not None:
        return None
    return allowed


async def _file_decisions(db: AsyncSession, events: Sequence[DecisionEvent]) -> None:
    sink = OutboxDecisionSink()
    for event in events:
        if event.decision.allowed:
            await sink.record_allow(db, event)
        else:
            await sink.record_deny(db, event)


__all__ = ["authorized_node"]
