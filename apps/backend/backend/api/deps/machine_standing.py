"""The one decision for a request a box makes on its machine's own standing.

The routing feed, its event stream and the worker-credential mint are the box's
root process acting as the machine itself: the bearer must be a live machine
credential that holds a live machine, never an org-bound worker credential, and
the org the request concerns must be one that credential may act within.
Decided through ``compute.machine_credential`` so the answer and the decision
row are the same at every such door.
"""

from __future__ import annotations

from uuid import UUID

from alkera_core.auth.machine_credential_standing import live_machine_of
from alkera_core.authz import ActingContext, Action, Resource, ResourceType
from alkera_core.compute.machines import has_run_org_workers
from alkera_core.compute.workspace_lease import holds_work_in
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import ComputeAllocation
from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.authz import enforce


async def decide_machine_standing(
    request: Request,
    db: AsyncSession,
    ctx: ActingContext,
    *,
    action: Action,
    resource_id: str,
    org_id: UUID,
    org_reached: bool,
) -> UUID:
    """Enforce the decision; return the machine the credential holds. A
    person is refused as needing a machine credential (401), a dead credential
    as refused (401), a worker credential and an org the credential may not act
    within as not-found (404). ``org_reached`` is whether ``org_id`` is one the
    machine reaches: its own for a request about the machine itself, an org
    it holds work in for a worker credential's mint."""
    held = (
        await live_machine_of(db, ctx.credential_id)
        if ctx.is_machine and ctx.credential_id
        else None
    )
    machine = await db.get(ComputeAllocation, held) if held is not None else None
    await enforce(
        request,
        db,
        ctx,
        action,
        Resource(ResourceType.MACHINE_CREDENTIAL, id=resource_id, org_id=org_id),
        {
            "is_machine": ctx.is_machine,
            "credential_live": held is not None,
            "machine_matches": held is not None and str(held) == ctx.acting_principal.id,
            # Every request decided here is the machine's own standing, which
            # a box keeps whether or not it runs a worker per org.
            "own_standing": True,
            "runs_org_workers": machine is not None and has_run_org_workers(machine),
            "org_reached": org_reached,
        },
    )
    if held is None:
        # enforce() refused every such case; the check keeps the types honest.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Machine not found")
    return held


async def org_held_by(ctx: ActingContext, org_id: UUID) -> bool:
    """Whether the machine ``ctx`` speaks as holds work in ``org_id``, by the
    one rule the worker door re-asks on every request (:func:`holds_work_in`):
    a live chat of the org bound to it, a workspace of the org it still holds,
    or a live Files lease of the org it is finishing on. So a box mid hand-back
    can mint the worker that completes it, and the mint never admits an org
    the door would then refuse. ``False`` for anything not a machine.

    Asked, as the door asks it, on a session held to no org: the request's
    own session reads only the orgs the box's session is held to
    (``held_orgs_query``), which a lease it is finishing on does not add."""
    if not ctx.is_machine:
        return False
    async with AsyncSessionLocal() as session:
        return await holds_work_in(session, machine_id=ctx.acting_principal.id, org_id=org_id)


__all__ = ["decide_machine_standing", "org_held_by"]
