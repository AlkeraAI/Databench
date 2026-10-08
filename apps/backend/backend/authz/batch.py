"""decide_many(): one decision per resource, one row per batch.

A route that decides about more than one resource is rendering a listing or
filtering a set of scopes, not attempting one access: it shows what the caller
may read and leaves the rest out. Deciding each row through :func:`enforce`
would raise on the first refusal and file a decision row per row, so a page of
a thousand entries would cost a thousand audit rows and stop at the first one
the caller may not see. This module is the batch shape of the same decision:

* every resource is decided by the same pure engine and the same policy
  :func:`enforce` uses, so a row a listing shows is exactly a row a single
  request for it would be allowed;
* nothing raises per resource: the caller gets every decision back and filters;
* the batch leaves ONE ``authz.decision`` row (:class:`BatchDecisionEvent`):
  the action, the resource type, how many were allowed and refused, and which
  were refused.

The facts a policy decides over come from one of two places. A caller that has
already read the rows (a Files listing that renders the node and its chain
from the same read) passes ``attrs_for``. Any other caller leans on the
resolver its resource type registered with :func:`register_batch_facts`, which
reads the facts for the whole batch at once; a resource type gains batch
listings by registering one, without a new signature here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Protocol

from alkera_core.authz import (
    ActingContext,
    Action,
    BatchDecisionEvent,
    Decision,
    Resource,
    ResourceType,
    authorize,
)
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.authz.enforce import DecisionSink, default_sink

#: The facts for one resource, as the caller resolved them.
AttrsFor = Callable[[Resource], Mapping[str, object]]


class BatchFacts(Protocol):
    """Reads the facts for a whole batch of one resource type at once.

    Returns the facts per resource id. A resource the resolver leaves out is
    decided over no facts at all, which every policy refuses: a resolver that
    cannot see a row never makes it readable by omission.
    """

    def __call__(
        self,
        request: Request,
        db: AsyncSession,
        ctx: ActingContext,
        resources: Sequence[Resource],
    ) -> Awaitable[Mapping[str, Mapping[str, object]]]: ...


_resolvers: dict[ResourceType, BatchFacts] = {}


def register_batch_facts(resource_type: ResourceType, resolver: BatchFacts) -> None:
    """Make ``resolver`` the batch fact source for ``resource_type``.

    One resolver per type: a second registration is a programming error, the
    same way a second policy for a type is, because two sources of facts for
    one type would be two answers to one question.
    """
    if resource_type in _resolvers:
        raise ValueError(f"a batch fact resolver for {resource_type.value} is already registered")
    _resolvers[resource_type] = resolver


def batch_facts_for(resource_type: ResourceType) -> BatchFacts | None:
    """The resolver registered for ``resource_type``, if any."""
    return _resolvers.get(resource_type)


async def decide_many(
    request: Request,
    db: AsyncSession,
    ctx: ActingContext,
    action: Action,
    resources: Sequence[Resource],
    attrs_for: AttrsFor | None = None,
    *,
    sink: DecisionSink | None = None,
) -> dict[str, Decision]:
    """Decide ``action`` on every resource; record one summary row; never raise
    for a refusal.

    Every resource must be of one type (the summary row is about one type) and
    the result is keyed by resource id. ``attrs_for`` gives the facts for one
    resource; without it the type's registered :class:`BatchFacts` resolver
    reads them for the whole batch, and a type with neither is a programming
    error rather than a silent refusal of everything.

    An empty batch decides nothing and records nothing.
    """
    if not resources:
        return {}
    kinds = {resource.type for resource in resources}
    if len(kinds) != 1:
        raise ValueError("decide_many decides one resource type per call")
    (kind,) = kinds
    facts = attrs_for or await _registered_facts(request, db, ctx, kind, resources)

    decisions: dict[str, Decision] = {}
    for resource in resources:
        decisions[resource.id] = authorize(ctx, action, resource, facts(resource))

    refused = tuple(rid for rid, decision in decisions.items() if not decision.allowed)
    first = resources[0]
    await (sink or default_sink()).record_batch(
        db,
        BatchDecisionEvent(
            org_id=first.org_id or ctx.org_id,
            actor=ctx.audit_dict(),
            action=action,
            resource_type=kind.value,
            allowed=len(decisions) - len(refused),
            refused_ids=refused,
            method=request.method,
            path=request.url.path,
        ),
    )
    return decisions


async def _registered_facts(
    request: Request,
    db: AsyncSession,
    ctx: ActingContext,
    kind: ResourceType,
    resources: Sequence[Resource],
) -> AttrsFor:
    resolver = _resolvers.get(kind)
    if resolver is None:
        raise LookupError(
            f"no batch fact resolver is registered for {kind.value}; "
            "pass attrs_for or register one with register_batch_facts"
        )
    by_id = await resolver(request, db, ctx, resources)
    return lambda resource: by_id.get(resource.id, {})


__all__ = [
    "AttrsFor",
    "BatchFacts",
    "batch_facts_for",
    "decide_many",
    "register_batch_facts",
]
