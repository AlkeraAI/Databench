"""Authorization glue the trash, versions and delta routes share.

``alkera_core.files.authz.authorize`` takes the platform ``enforce`` as a
parameter so the library never imports the backend. Every route therefore has
to hand it the same two things — the caller's resolved role facts and a closure
over ``enforce`` bound to this request — and each one would otherwise spell them
out again. Spelled once here, so a lane cannot accidentally resolve *different*
facts and get a different decision for the same caller.

The enforcer itself is ``backend.api.deps.files.files_enforcer``, shared with every other
family: it is the one place the two error vocabularies meet, and a second copy
of that translation is how the three "not yours" classes drift apart again.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from alkera_core.authz.principal import ActingContext
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.authorize import (
    Authorized,
    authorize,
    node_resource,
    platform_action,
    present_attrs,
)
from alkera_core.files.authz.decider import AccessFacts
from alkera_core.files.authz.readable import DecidedNode, decided_by_id
from alkera_core.files.ids import NodeId
from alkera_core.files.repo import FilesRepo
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps.files import as_platform, files_enforcer, platform_wrap
from backend.authz import decide_many, role_resolver
from backend.services.files.facts import caller_facts, verified_machine_id


async def facts_for(
    request: Request, db: AsyncSession, ctx: ActingContext, *, machine_id: str | None = None
) -> AccessFacts:
    """The caller's role facts for their own org: every team they are in, and
    the subset they administer.

    The resolution itself is :func:`backend.services.files.facts.caller_facts`,
    shared with the callers that have no request to resolve from (the Slack
    relay reads a produced image as the thread's owner off a sweep, not off a
    request). This is the route spelling: the resolver cached on the request,
    so a route asking about several teams walks each ancestor chain once.

    The machine half is the one fact here that is not about roles at all. It is
    :func:`backend.services.files.facts.verified_machine_id` — the assertion
    checked against the registration — and it is resolved there rather than
    here because the lease layer derives the holder it fences on from the same
    answer, and two resolutions of "which machine is this" would be two
    different fences. Read inside the platform window this resolution already
    runs in, because the table it reads is a platform table the Files role
    cannot see. Only an agent pays for it.

    ``machine_id`` is for the one door that cannot do that check: the content
    origin carries no credential, so it presents the machine the SERVER signed
    into the capability URL at mint time instead. Nothing a request can choose
    reaches it — an unsigned or edited claim never survives the MAC — and
    ``None`` means "resolve it from this request", which is what every other
    door passes.

    Per-node facts — ``leased_subtree``, the lock holder, the node flags — are
    the caller's to fill in: they depend on the node, this does not.
    """
    facts = await caller_facts(db, ctx, role_resolver(request, db, ctx))
    if not (ctx.is_agent or ctx.is_machine):
        return facts
    proven = machine_id if machine_id is not None else await verified_machine_id(db, ctx)
    return replace(facts, agent_machine_id=proven)


#: The name the trash, versions, delta and feeds lanes already import.
access_facts = facts_for


async def authorized(
    request: Request,
    db: AsyncSession,
    repo: FilesRepo,
    ctx: ActingContext,
    node_id: NodeId,
    action: FilesAction,
    *,
    facts: AccessFacts | None = None,
) -> Authorized[Any]:
    """Decide ``action`` on ``node_id`` through the platform engine, or raise.

    Called inside the family's Files transaction, which runs as the restricted
    Files role: the facts read and the decision row both step out of that role
    through ``as_platform``, so a caller that resolves its own ``facts`` has to
    read them under the same window.
    """
    if facts is None:
        async with as_platform(db):
            facts = await access_facts(request, db, ctx)
    return await authorize(
        ctx,
        repo,
        node_id,
        action,
        facts=facts,
        enforce=files_enforcer(request, db, wrap=platform_wrap(db)),
    )


async def decided_page(
    request: Request,
    db: AsyncSession,
    repo: FilesRepo,
    ctx: ActingContext,
    node_ids: Iterable[NodeId],
    action: FilesAction,
    *,
    facts: AccessFacts,
) -> dict[uuid.UUID, DecidedNode]:
    """The rows of a page the caller may ``action``, decided in one batch.

    The rows, their chains and their access come from one batched read
    (:func:`~alkera_core.files.authz.readable.decided_by_id`); the verdict on
    each comes from the ``files.access`` policy over the same facts a single
    :func:`authorized` call decides on, through
    :func:`backend.authz.decide_many`, which leaves one summary row for the
    page. A refused row, and an id that is not there, are simply absent: a
    listing filters, it never raises per row.
    """
    rows = await decided_by_id(repo, ctx, node_ids, facts=facts)
    by_id = {str(node_id): row for node_id, row in rows.items()}
    decisions = await decide_many(
        request,
        db,
        ctx,
        platform_action(action),
        [node_resource(row.node, row.drive) for row in rows.values()],
        lambda resource: present_attrs(
            by_id[resource.id].access,
            by_id[resource.id].node,
            by_id[resource.id].drive,
            ctx,
            facts,
        ),
    )
    return {node_id: row for node_id, row in rows.items() if decisions[str(node_id)].allowed}


__all__ = ["access_facts", "authorized", "decided_page", "facts_for"]
