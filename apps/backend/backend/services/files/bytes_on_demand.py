"""Ask the machine holding a folder for one file's bytes -- the one way to.

A file under a live lease can be on the drive before its bytes are: the holder
reports its tree ahead of its uploads. A reader of such a file -- a person
opening it in the web app, the Slack relay about to post it into a thread --
asks the holder for that one file through the process's
:class:`~alkera_core.files.promotion.Promoter` and waits for the bytes to land
through the ordinary fenced upload. Both readers ask through
:func:`promote_under`, so a machine is asked the same way, capped the same way
and answered the same way whoever is reading.
"""

from __future__ import annotations

from uuid import UUID

from alkera_core.files import lease_snapshots
from alkera_core.files.authz.authorize import Authorized
from alkera_core.files.promotion import PromoteOutcome, Promoter
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.files.context import FilesContext


def lease_relative_path(allowed: Authorized[str], lease_node_id: UUID) -> str | None:
    """The node's path under the leased folder, as the holder's tree report
    spells it: the names below the lease root, ``/``-joined, as UTF-8.

    ``None`` when the lease root is not on the node's chain, or a name is not
    UTF-8 (a path the box could not have reported in that spelling)."""
    names: list[bytes] = []
    below = False
    for ancestor in allowed.chain:
        if below and ancestor.id != allowed.node.id:
            names.append(bytes(ancestor.name))
        if ancestor.id == lease_node_id:
            below = True
    if not below:
        return None
    names.append(bytes(allowed.node.name))
    try:
        return b"/".join(names).decode("utf-8")
    except UnicodeDecodeError:
        return None


async def promote_under(
    db: AsyncSession,
    allowed: Authorized[str],
    lease: lease_snapshots.LeaseFacet,
    *,
    promoter: Promoter | None,
    deadline: float,
    asking_machine: str | None = None,
) -> PromoteOutcome:
    """Ask the lease's machine for the node's bytes and wait for them.

    ``asking_machine`` is the machine the request proved it is, if any. When
    it is the lease's own machine nothing is asked: the holder reading a file
    it has not sent yet would be waiting on itself.

    The caller's session is committed first, so no connection is held while
    the wait parks: the wait is on the hub and nowhere else. A process that
    could not hear the answer (``promoter`` is ``None``: no running listener)
    asks nothing and answers ``timed_out`` at once; a node whose path under the
    lease cannot be spelled asks nothing and answers ``missing``."""
    if not lease.live or lease.served != "live":
        return PromoteOutcome.OFFLINE
    if asking_machine is not None and asking_machine == lease.machine:
        return PromoteOutcome.REQUESTER
    path = lease_relative_path(allowed, lease.node_id)
    if path is None:
        return PromoteOutcome.MISSING
    if promoter is None:
        return PromoteOutcome.TIMED_OUT
    await db.commit()
    return await promoter.promote(allowed.node, lease, deadline=deadline, path=path)


async def ask_holder(
    db: AsyncSession,
    files: FilesContext,
    allowed: Authorized[str],
    *,
    promoter: Promoter | None,
    deadline: float,
) -> PromoteOutcome:
    """:func:`promote_under` for a caller that has not read the lease yet: the
    node's lease facet, then the ask. A node no machine holds is ``offline``."""
    async with files.repo.transaction():
        lease = await lease_snapshots.lease_facet(
            files.repo, allowed.node, allowed.chain, ctx=files.ctx, now=files.clock.now()
        )
    if lease is None:
        return PromoteOutcome.OFFLINE
    return await promote_under(db, allowed, lease, promoter=promoter, deadline=deadline)


__all__ = ["ask_holder", "lease_relative_path", "promote_under"]
