"""How a Files route loads and authorizes a node, fences a write, and reads wire ids and names."""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import unquote_to_bytes
from uuid import UUID

from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.authorize import Authorized, Read, authorize
from alkera_core.files.authz.decider import AccessFacts
from alkera_core.files.errors import InvalidRequest, NotFound
from alkera_core.files.filters import Direction, OrderBy, OrderField
from alkera_core.files.ids import NodeId
from alkera_core.files.lease_snapshots import HeldLease, live_holders_under
from fastapi import Request

from backend.api.deps.files import (
    Enforcer,
    LeaseContext,
    as_platform,
    files_enforcer,
    platform_wrap,
)
from backend.api.deps.files_facts import facts_for
from backend.authz.enforce import DecisionSink
from backend.services.files.context import FilesContext
from backend.services.realtime import flush_holders


async def _resolve(
    request: Request,
    files: FilesContext,
    drive_id: uuid.UUID,
    node_id: uuid.UUID,
    action: FilesAction,
    *,
    sink: DecisionSink | None = None,
    facts: AccessFacts | None = None,
) -> Authorized[Any]:
    """Load ``node_id`` and decide ``action`` on it, or raise.

    ``drive_id`` is checked *after* the decision, and by raising the same
    :class:`NotFound`, so naming a real node under the wrong drive is not a way
    to learn that the node exists.

    ``facts`` is a parameter so a handler that needs the caller's memberships
    again — a listing that has to cut its page by the same decision — resolves
    them once instead of paying the membership read twice per request.
    """
    if facts is None:
        async with as_platform(files.repo.session):
            facts = await facts_for(request, files.repo.session, files.ctx, files.drive)
    decided = await authorize(
        files.ctx,
        files.repo,
        NodeId(node_id),
        action,
        facts=facts,
        enforce=_enforcer(request, files, sink=sink),
    )
    if decided.node.drive_id != drive_id:
        raise NotFound()
    return decided


def _enforcer(
    request: Request, files: FilesContext, *, sink: DecisionSink | None = None
) -> Enforcer:
    """The shared binding, stepping out of the tenant role for its statements.

    Binding here rather than passing the request down into the library is what
    keeps ``alkera_core.files`` free of FastAPI: the library asks for a callable
    of four arguments and gets one.
    """
    session = files.repo.session
    return files_enforcer(request, session, wrap=platform_wrap(session), sink=sink)


def files_txn(files: FilesContext) -> Any:
    """The one transaction a handler runs in.

    Authorization, the mutation and the decision row share it on purpose: an
    allow that was written but whose mutation rolled back would be a record of
    something that never happened, and a mutation that committed without its
    decision row would be an unrecorded one.
    """
    return files.repo.transaction()


def _name_bytes(name: str) -> bytes:
    """A wire name as the bytes the tree stores.

    Names are bytes end to end, never decoded text; a client that has a name it
    cannot express in UTF-8 sends it percent-encoded, which is exactly what the
    path form does too, so one decoder serves both.
    """
    return unquote_to_bytes(name)


def _uuid(raw: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise InvalidRequest("files.bad_id", f"{field} is not an id this server issued") from None


def _fence(lease: LeaseContext, files: FilesContext) -> Any:
    """The library's lease context, or ``None`` for an ordinary write.

    The caller rides with the epoch: the two headers name a lease, and only the
    identity the server derived for this request says that lease is theirs —
    the credential's own principal, or, for a box, the machine it proved. An
    assertion the request merely sent is not part of it.

    The namespace verbs reached through here spend the drive's node count, and
    a fenced request spends it like anybody else's: holding a folder is not a
    reason to mint nodes past the ceiling, and the live plane creates under the
    same fence continuously. What the release itself carries is applied by the
    server, under the context the lease service builds for it.
    """
    if not lease.fenced:
        return None
    from alkera_core.files.leases import LeaseContext as LibraryLease

    return LibraryLease(epoch=lease.epoch, instance_id=lease.instance, holder=files.holder)


def _order(raw: str | None) -> OrderBy:
    """`orderBy=name`, `orderBy=size desc`, `orderBy=mtime asc`."""
    if not raw:
        return OrderBy()
    parts = raw.strip().split()
    try:
        field = OrderField(parts[0])
    except ValueError:
        raise InvalidRequest("files.bad_order", f"cannot order by {parts[0]!r}") from None
    if len(parts) == 1:
        return OrderBy(field=field)
    try:
        return OrderBy(field=field, direction=Direction(parts[1]))
    except ValueError:
        raise InvalidRequest("files.bad_order", "order direction must be asc or desc") from None


async def _authorized_node(
    request: Request, files: FilesContext, node_id: UUID, action: FilesAction
) -> Read:
    """Decide ``action`` on one Files node for this caller, or raise.

    Drive-agnostic on purpose: a chat names a node, not a path, and the repo is
    already scoped to the caller's org — so a node that does not exist, one in
    another org and one the caller may not read all come back as the same
    :class:`NotFound`, which ``backend.api.deps.files_errors`` renders as an
    opaque 404. Appearing in a transcript is not a grant: this runs on every
    request that touches the node, never once at attach time.
    """
    async with as_platform(files.repo.session):
        facts = await facts_for(request, files.repo.session, files.ctx, files.drive)
    session = files.repo.session
    async with files.repo.transaction():
        return await authorize(
            files.ctx,
            files.repo,
            NodeId(node_id),
            action,
            facts=facts,
            enforce=files_enforcer(request, session, wrap=platform_wrap(session)),
        )


class _RehearsalOverError(Exception):
    """Ends the read-only pass before a trash, rolling back what it wrote."""


async def flush_holders_under(
    request: Request, files: FilesContext, drive_id: str, item_id: str
) -> int:
    """Before a trash of the item: have every machine holding a folder at or
    under it push what it holds (:func:`backend.services.realtime.flush_holders`).

    The machines are found by a rehearsal of the trash's own authorization,
    rolled back so its allow is not on record twice (the trash decides again
    and records that one), and asked only for a caller the trash would admit:
    one who may not write the item is refused here exactly as the trash would
    refuse them, having done the same work. Answers how many said they had."""
    found: list[HeldLease] = []
    try:
        async with files_txn(files):
            decided = await _resolve(
                request, files, _uuid(drive_id, "drive"), _uuid(item_id, "item"), FilesAction.WRITE
            )
            found = await live_holders_under(files.repo, decided.node, now=files.clock.now())
            raise _RehearsalOverError
    except _RehearsalOverError:
        pass
    return await flush_holders(request.app, found)
