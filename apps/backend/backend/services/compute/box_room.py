"""Whether a box may take another org's chats: what its last heartbeat said
it runs (a worker per org, proven isolation, a worker budget, free slots)
and which orgs already have chats bound to it. Pure reads of the
allocation row, plus the one query that counts the orgs bound to a box.
Placement decides with these; it re-exports them for its callers."""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from alkera_core.compute.box_contract import BoxCapability
from alkera_core.compute.box_isolation import IsolationProfile, profile_from_capabilities
from alkera_core.compute.machines import failing_orgs, has_run_org_workers
from alkera_core.models import ComputeAllocation, WorkspaceObject
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def has_capability(alloc: ComputeAllocation, capability: str) -> bool:
    """Whether the box said on its last heartbeat that it can do ``capability``.
    A box that never said anything (a daemon on the previous build) can do
    nothing new."""
    return capability in (alloc.capabilities_json or ())


def rolled_back_from_org_workers(alloc: ComputeAllocation) -> bool:
    """Whether the box once ran a worker per org and its current process does
    not: a rollback to a build that serves every org from one process. Its
    machine credential is refused every chat and data route
    (:func:`~alkera_core.compute.machines.has_run_org_workers`), so it can
    serve no chat, and placement gives it none until a build that runs a
    worker per org beats again."""
    return has_run_org_workers(alloc) and not runs_org_workers(alloc)


def runs_org_workers(alloc: ComputeAllocation) -> bool:
    """Whether the box said on its last heartbeat that it runs each org in a
    worker process of that org's own. A box that did not runs every chat it
    holds in one process, under one home and one state tree."""
    return has_capability(alloc, BoxCapability.ORG_WORKERS)


def isolates_orgs(alloc: ComputeAllocation) -> bool:
    """Whether the box proved, on its last beat, that it keeps the orgs it
    serves apart (``alkera_core.compute.box_isolation``): it runs a worker per
    org and names ``org_isolation``. A box that does not (a container
    provider's pod, an older build, one that said nothing) is single-org."""
    return runs_org_workers(alloc) and (
        profile_from_capabilities(alloc.capabilities_json) == IsolationProfile.ORG_NAMESPACES
    )


def org_worker_capacity(alloc: ComputeAllocation) -> int:
    """How many orgs the box said it can run a worker for at once, or 0 for a
    box that reported no budget (not gated)."""
    raw = (alloc.resources_json or {}).get("org_worker_capacity")
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 0


def org_slots_free(alloc: ComputeAllocation) -> int | None:
    """The worker slots a new org can still take on the box, as its last
    sample said, or ``None`` when it said nothing (an older box: not gated)."""
    raw = (alloc.resources_json or {}).get("org_slots_free")
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else None


async def bound_orgs(db: AsyncSession, machine_ids: Iterable[UUID]) -> dict[UUID, set[UUID]]:
    """Which orgs have a live chat bound to each machine: each is a worker the
    box runs (or will, the moment the chat is opened), counted from the chats
    so a placement made since the box's last report is already charged."""
    wanted = [str(machine_id) for machine_id in machine_ids]
    if not wanted:
        return {}
    bound = WorkspaceObject.spec["machine_id"].astext
    rows = await db.execute(
        select(bound, WorkspaceObject.org_team_id)
        .where(
            WorkspaceObject.type == "chat",
            WorkspaceObject.deleted_at == 0,
            bound.in_(wanted),
        )
        .distinct()
    )
    found: dict[UUID, set[UUID]] = {}
    for machine, org in rows.all():
        found.setdefault(UUID(str(machine)), set()).add(org)
    return found


def has_worker_room(alloc: ComputeAllocation, orgs_here: set[UUID], org_id: UUID | None) -> bool:
    """Whether ``alloc`` may take a chat of ``org_id``, given the orgs whose
    chats are bound to it now. An org already there adds nothing.

    A box that does not prove it keeps orgs apart (:func:`isolates_orgs`:
    one that serves every chat in one process, or runs a worker per org on a
    host that gives it no cgroup, mount, user or network namespace to put the
    worker in) takes a new org only while it holds none, whatever its
    tenancy: a second org there would share the first org's processes and
    files. A box that does takes a new org while the worker budget it
    reported is not spent (no budget reported is not gated). A box whose last
    sample said no worker slot is free takes no new org, whatever it runs;
    one that never reports slots is not gated by them. A single-tenant box
    (an org's own, dedicated or personal) is otherwise its one tenant's
    whatever it runs. A box whose worker for the org keeps failing takes none
    of its chats, even one it held.
    """
    if rolled_back_from_org_workers(alloc):
        return False
    if org_id is not None and str(org_id) in failing_orgs(alloc):
        return False
    if org_id is not None and org_id in orgs_here:
        return True
    if org_slots_free(alloc) == 0:
        return False
    if not isolates_orgs(alloc):
        return not orgs_here
    capacity = org_worker_capacity(alloc)
    return capacity == 0 or len(orgs_here) < capacity


__all__ = [
    "bound_orgs",
    "has_capability",
    "has_worker_room",
    "isolates_orgs",
    "org_slots_free",
    "org_worker_capacity",
    "rolled_back_from_org_workers",
    "runs_org_workers",
]
