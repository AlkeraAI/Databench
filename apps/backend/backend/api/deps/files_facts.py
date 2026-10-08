"""The drive half of a caller's Files facts, which every route that authorizes a node builds."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime

from alkera_core.authz.principal import ActingContext
from alkera_core.files.authz.decider import AccessFacts
from alkera_core.files.leases import holder_identity, leases_held_by
from alkera_core.models.files.stores import FileDrive
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps.files import lease_context
from backend.api.deps.files_lane import facts_for as lane_facts
from backend.services.files.facts import drive_confinement


async def facts_for(
    request: Request,
    db: AsyncSession,
    ctx: ActingContext,
    drive: FileDrive,
    *,
    now: datetime | None = None,
    machine_id: str | None = None,
) -> AccessFacts:
    """The caller's facts for one drive — the only way any Files route builds them.

    The caller half (which teams, which of them they administer, whether they
    are an org admin) is :func:`backend.api.deps.files_lane.facts_for`,
    resolved from the *materialized* membership rows: a grant to a sub-team
    matches because the caller really is a member of that team. Deriving the
    set from the org root's ancestor chain — the shape every family carried
    its own copy of — yields the org id alone, so every sub-team grant matched
    nothing and the node stayed unreadable to the people it was shared with.

    The drive half is the agent confinement: without ``leased_subtree`` the
    decider empties an agent's action set on every node, so an agent could do
    nothing anywhere except through the one family that filled this in — and
    without ``chats_bound_elsewhere`` a box proven on its own chat is proven on
    every chat in the drive, because one credential serves them all.
    """
    caller = await lane_facts(request, db, ctx, machine_id=machine_id)
    confined = await drive_confinement(db, ctx, caller, drive)
    return replace(
        confined,
        held_leases=await _held_leases(request, db, ctx, drive, caller.agent_machine_id),
        now=now,
    )


async def _held_leases(
    request: Request,
    db: AsyncSession,
    ctx: ActingContext,
    drive: FileDrive,
    machine_id: str | None,
) -> frozenset[uuid.UUID]:
    """The subtrees this request proved it holds: the lease headers it sent,
    checked against the live lease rows.

    The pair alone proves nothing — the machine id rides every chat row the box
    serves and the epoch is a small counter — and neither does the assertion
    that names the machine, which is two headers any member may put on their
    own session. So the rows are matched on the identity the SERVER derived:
    for an agent that is the machine ``machine_id`` names, which is filled in
    only once the assertion has been checked against the registration. A
    colleague replaying a box's headers, the box's public id included, holds no
    lease and reads none.

    Only an agent's word is worth the read — the decider admits nobody else
    through it (a person who mounted a chat folder still may not download the
    chat) — and a request that named no lease holds none. The rows are read
    here, in the platform window every route builds its facts in, so the fact
    reaches the item, the listing and the content route from one place: the
    ``canDownload`` a box reads off a listing row is the answer the content
    route will give it.
    """
    if not (ctx.is_agent or ctx.is_machine):
        return frozenset()
    holder = holder_identity(ctx, verified_machine_id=machine_id)
    if holder is None:
        return frozenset()
    lease = await lease_context(request)
    if lease.epoch is None or lease.instance is None:
        return frozenset()
    return await leases_held_by(
        db,
        org_team_id=drive.org_team_id,
        epoch=lease.epoch,
        instance_id=lease.instance,
        holder=holder,
    )
