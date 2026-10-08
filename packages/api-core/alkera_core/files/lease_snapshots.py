"""What a leased folder pushes, and what everyone else sees while it holds.

Two halves of one story. The exporter half — `SnapshotSpec` and the batch that
applies it — is what the holder sends; the reader half — `lease_facet` — is what
every other client renders on every item under the mount, so the browser can say
"Ana has this folder mounted on her laptop, last synced 12 s ago" without asking
whether the folder itself is the leased one.

The facet deliberately does not come from the node's own row. A lease is held on
a folder and governs its whole subtree, so the answer for a file six levels down
is the lease on its nearest leased ancestor. Reading it from the chain — which
every read path has already loaded to authorize — means the facet costs one
query for a page of items rather than one per item, and means a node cannot
disagree with its parent about who holds it.

`stale` is server-computed and clock-driven rather than stored, because it is a
statement about *now*: a holder that is alive (its heartbeat keeps arriving) but
behind (`last_sync_at` has stopped advancing) has been failing to push for longer
than two sync intervals. Storing it would mean a row that has to be re-written
every time the clock crosses the threshold; deriving it means the same lease row
reads fresh at one moment and stale at the next with no writer involved.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import text

from alkera_core.authz.principal import ActingContext
from alkera_core.config import get_settings
from alkera_core.files.ids import NodeId
from alkera_core.files.lease_live import LiveEntriesService, LiveFacet, LivePage
from alkera_core.files.repo import SUBTREE_DEPTH_BIND, FilesRepo, subtree_sql
from alkera_core.models.files.tree import FileNode

#: How often a holder is expected to push a snapshot. `stale` is two of these,
#: so one missed batch never raises the flag and two consecutive ones always do.
DEFAULT_SYNC_INTERVAL: Final = timedelta(seconds=30)

#: The multiple of the sync interval a holder may fall behind before every
#: reader is told it is behind. Two, so the flag means "missed twice", which is
#: a holder in trouble rather than a holder mid-batch.
STALE_INTERVALS: Final = 2

#: How many heartbeat cadences a holder may go quiet before its folder reads
#: ``offline``. A lease outlives a dead holder by its whole TTL -- minutes --
#: and for all of them a reader would otherwise be told a machine was serving
#: the folder. Three, so one late beat never flips it and a dead box does
#: within half a minute at the default cadence.
SERVED_HEARTBEATS: Final = 3

#: The ``subtype`` the bridge stamps on a chat's own folder: the workspace
#: object's type, verbatim (``objects_bridge`` writes ``subtype=workspace_object.type``).
#: Spelled here rather than imported because ``alkera_core.files`` re-exports
#: this module, and a test pins the two spellings against each other.
CHAT_FOLDER_SUBTYPE: Final = "chat"


@dataclass(frozen=True, slots=True)
class LeaseFacet:
    """The `lease` block on an item payload, plus the `stale` beside it.

    Ids and machine names only: a facet is rendered to anyone who can read the
    item, which is a wider audience than the people who may read the leased
    folder's name, so it carries nothing that would name the folder itself.
    The one identity it does carry is the chat whose folder is leased, by id:
    a box takes a chat's folder under the ``chat`` purpose, and a reader inside
    that folder is told which conversation holds it. Whether they may open it
    is the chat policy's answer, resolved by the route, never here.
    """

    node_id: NodeId
    holder_principal_id: uuid.UUID
    machine: str
    purpose: str
    since: datetime
    expires_at: datetime
    last_sync_at: datetime | None
    #: True when the caller is the holder — the client uses it to decide whether
    #: to offer "unmount" or "ask for it back".
    mine: bool
    #: The holder is alive but has not pushed in over two sync intervals.
    stale: bool
    #: The holder is streaming what it writes as it writes it. A lease without
    #: this is still a lease — the subtree is fenced — but the drive's copy only
    #: moves at a checkpoint, so nothing under it is ever newer than
    #: ``last_sync_at`` and nothing is ever in flight.
    live: bool = False
    #: A write by someone other than the holder is admitted into the subtree and
    #: handed to the holder, rather than refused.
    inbound: bool = False
    #: How many nodes under the lease are in flight right now.
    pending: int = 0
    #: Bumped by every change to the live plane.
    live_seq: int = 0
    #: What THIS node is doing, when it is one of the ones in flight. ``None``
    #: for the overwhelming majority of rows: a node merely inside a streaming
    #: mount is not itself moving.
    live_state: LiveFacet | None = None
    #: The chat whose own folder the lease is on, when it is on one. A lease
    #: on any other folder names no chat, whatever purpose it was taken under.
    chat_id: uuid.UUID | None = None
    #: ``live`` while the lease runs the live plane and its holder has beaten
    #: within :data:`SERVED_HEARTBEATS` of its cadence; ``offline`` otherwise.
    served: str = "offline"
    #: Files under the lease whose bytes are still on their way to the drive.
    landing_count: int = 0
    #: The lease's current epoch: the fence its holder writes under. A request
    #: to the holder names it, so a box holding the folder at an older epoch —
    #: from before a handover — refuses it rather than serving it.
    epoch: int = 0


def is_stale(
    heartbeat_at: datetime,
    last_sync_at: datetime | None,
    *,
    acquired_at: datetime,
    sync_interval: timedelta = DEFAULT_SYNC_INTERVAL,
) -> bool:
    """Whether the holder is beating but no longer pushing.

    A lease that has never synced is measured from when it was acquired, so a
    mount that beats for five minutes without ever pushing a batch is stale for
    the same reason one that stopped pushing is: the last time this folder's
    contents were known good is too far behind the last time the holder was
    known alive.
    """
    since_sync = last_sync_at if last_sync_at is not None else acquired_at
    return heartbeat_at - since_sync > STALE_INTERVALS * sync_interval


async def lease_facet(
    repo: FilesRepo,
    node: FileNode,
    chain: Sequence[FileNode],
    *,
    ctx: ActingContext,
    now: datetime,
    sync_interval: timedelta = DEFAULT_SYNC_INTERVAL,
) -> LeaseFacet | None:
    """The lease governing `node`: its own, or its nearest leased ancestor's.

    `chain` is the ancestor chain the read path already loaded, root-first;
    whether it includes `node` itself does not matter, because the node's id is
    added here either way. `now` is the caller's clock rather than Postgres's,
    because a listing renders one consistent moment for every item on the page —
    two items on one page must never disagree about whether the mount is stale.
    """
    ids = [n.id for n in chain]
    ids.append(node.id)
    rows = (
        await repo.session.execute(
            text(
                "SELECT l.*, n.depth AS lease_depth, "
                "n.target_object_id AS lease_object_id, n.subtype AS lease_subtype "
                "FROM file_leases l JOIN file_nodes n ON n.id = l.node_id "
                "WHERE l.org_team_id = :org AND l.node_id = ANY(:ids) "
                "AND l.released_at IS NULL AND l.reaped_at IS NULL AND l.expires_at > :now "
                "ORDER BY n.depth DESC LIMIT 1"
            ),
            {"org": repo.scope.org_team_id, "ids": ids, "now": now},
        )
    ).first()
    if rows is None:
        return None
    live = await _live_page(repo, ctx, [node.id], [rows], now=now)
    return _facet(rows, ctx=ctx, sync_interval=sync_interval, live=live, node_id=node.id, now=now)


async def lease_facets(
    repo: FilesRepo,
    entries: Sequence[tuple[uuid.UUID, Sequence[uuid.UUID]]],
    *,
    ctx: ActingContext,
    now: datetime,
    sync_interval: timedelta = DEFAULT_SYNC_INTERVAL,
) -> dict[uuid.UUID, LeaseFacet]:
    """The facet for every node on one page, in one statement.

    A listing renders many nodes that share ancestors, so asking
    :func:`lease_facet` per row would be one query per row for an answer that
    lives on the chain the page already loaded. Every candidate id — each row's
    own and every id on its chain — goes out in a single ``ANY``, and the
    deepest live lease among a row's own candidates is that row's facet, which
    is the same "nearest leased ancestor wins" rule the single-node form
    applies. Nodes under no live lease are absent from the mapping rather than
    present with ``None``, so a caller renders a facet only when there is one.

    An entry is ``(node id, candidate ids)`` — plain uuids rather than the ORM
    rows the single-node form takes — because a feed cannot hold its rows: it
    decides each candidate in turn and a refusal commits its decision in its
    own session, expiring the rows an earlier decision loaded. Ids read while
    the row was live survive that; the row objects do not.
    """
    candidates: set[uuid.UUID] = set()
    for node_id, chain_ids in entries:
        candidates.update(chain_ids)
        candidates.add(node_id)
    if not candidates:
        return {}
    rows = (
        await repo.session.execute(
            text(
                "SELECT l.*, n.depth AS lease_depth, "
                "n.target_object_id AS lease_object_id, n.subtype AS lease_subtype "
                "FROM file_leases l JOIN file_nodes n ON n.id = l.node_id "
                "WHERE l.org_team_id = :org AND l.node_id = ANY(:ids) "
                "AND l.released_at IS NULL AND l.reaped_at IS NULL AND l.expires_at > :now"
            ),
            {"org": repo.scope.org_team_id, "ids": list(candidates), "now": now},
        )
    ).all()
    if not rows:
        return {}
    by_node = {row.node_id: row for row in rows}
    deepest_for: dict[uuid.UUID, Any] = {}
    for node_id, chain_ids in entries:
        deepest: Any = None
        for candidate in (*chain_ids, node_id):
            row = by_node.get(candidate)
            if row is not None and (deepest is None or row.lease_depth > deepest.lease_depth):
                deepest = row
        if deepest is not None:
            deepest_for[node_id] = deepest
    if not deepest_for:
        return {}
    live = await _live_page(repo, ctx, list(deepest_for), list(deepest_for.values()), now=now)
    return {
        node_id: _facet(
            row, ctx=ctx, sync_interval=sync_interval, live=live, node_id=node_id, now=now
        )
        for node_id, row in deepest_for.items()
    }


def _is_live(row: Any) -> bool:
    """Whether the lease was granted the streaming plane.

    The cadence block is written on the lease row when — and only when — the
    holder asked for live sync and was given the numbers to run it at, so its
    presence IS the answer. Deriving it from the block rather than adding a
    second boolean beside it means a lease cannot be live and have no cadence,
    which is the one combination a holder could not act on.
    """
    return bool(row.live_cadence)


async def _live_page(
    repo: FilesRepo,
    ctx: ActingContext,
    row_ids: Sequence[uuid.UUID],
    lease_rows: Sequence[Any],
    *,
    now: datetime,
) -> LivePage:
    """The in-flight plane for the leases this page landed on, in one statement.

    Skipped entirely unless one of those leases is actually streaming, so an
    ordinary page — a drive with a desktop mount on it, or with none at all —
    costs exactly what it cost before, and a page that IS watching an agent work
    costs one statement more however many rows it has.
    """
    live_leases = [row.node_id for row in lease_rows if _is_live(row)]
    if not live_leases:
        return LivePage()
    return await LiveEntriesService(repo, ctx).facets_for_page(
        row_ids, lease_node_ids=live_leases, now=now
    )


def _facet(
    row: Any,
    *,
    ctx: ActingContext,
    sync_interval: timedelta,
    live: LivePage,
    node_id: uuid.UUID,
    now: datetime,
) -> LeaseFacet:
    return LeaseFacet(
        node_id=NodeId(row.node_id),
        holder_principal_id=row.holder_principal_id,
        machine=row.machine_id,
        purpose=row.purpose,
        since=row.acquired_at,
        expires_at=row.expires_at,
        last_sync_at=row.last_sync_at,
        # "Mine" compares the whole holder, kind included, against the
        # principal the server authenticated — so a caller who asserts an
        # agent id (a header anyone may send) is never told a machine's lease
        # is theirs, and a person's own mount still reads as their own.
        mine=(
            row.holder_kind == ctx.acting_principal.kind.value
            and str(row.holder_principal_id) == ctx.acting_principal.id
        ),
        stale=is_stale(
            row.heartbeat_at,
            row.last_sync_at,
            acquired_at=row.acquired_at,
            sync_interval=sync_interval,
        ),
        live=_is_live(row),
        inbound=bool(row.accepts_inbound),
        pending=live.pending.get(row.node_id, 0),
        live_seq=int(row.live_seq),
        live_state=live.facets.get(node_id),
        chat_id=_chat_of(row),
        served="live" if _is_live(row) and _beating(row, now=now) else "offline",
        landing_count=live.landing.get(row.node_id, 0),
        epoch=int(row.epoch),
    )


def _beating(row: Any, *, now: datetime) -> bool:
    """Whether the holder has beaten recently enough to be serving the folder.

    The cadence is the deployment's, read per call so a deployment that slows
    its holders' beat does not have every folder read offline between beats.
    """
    grace = timedelta(seconds=SERVED_HEARTBEATS * get_settings().files_lease_heartbeat_seconds)
    return bool(now - row.heartbeat_at <= grace)


def _chat_of(row: Any) -> uuid.UUID | None:
    """The chat the leased node IS, or ``None`` for any other folder.

    A chat's folder points at its object and is stamped with the object's type,
    so a lease on it names the conversation by the node's own columns; the row
    carries them from the join above. A working directory inside the chat, or a
    plain folder somebody mounted, points at nothing and names no chat.
    """
    if row.lease_subtype != CHAT_FOLDER_SUBTYPE or row.lease_object_id is None:
        return None
    return uuid.UUID(str(row.lease_object_id))


@dataclass(frozen=True, slots=True)
class HeldLease:
    """A live lease a machine is serving: the folder, its epoch, its machine.
    What a trash over the folder asks to push first."""

    org_id: uuid.UUID
    lease_node_id: uuid.UUID
    epoch: int
    machine: str


async def live_holders_under(repo: FilesRepo, node: FileNode, *, now: datetime) -> list[HeldLease]:
    """Every streaming lease on ``node`` or under it whose machine is beating:
    the holders whose unsent work a trash of ``node`` would take with it."""
    rows = (
        await repo.session.execute(
            text(
                "SELECT l.* FROM file_leases l JOIN file_nodes n ON n.id = l.node_id "  # noqa: S608
                "WHERE l.org_team_id = :org "
                "AND l.released_at IS NULL AND l.reaped_at IS NULL AND l.expires_at > :now "
                f"AND {subtree_sql('n.path_ids', 'CAST(:path AS ltree)')}"
            ),
            {
                "org": repo.scope.org_team_id,
                "path": node.path_ids,
                "now": now,
                **SUBTREE_DEPTH_BIND,
            },
        )
    ).all()
    return [
        HeldLease(
            org_id=repo.scope.org_team_id,
            lease_node_id=row.node_id,
            epoch=int(row.epoch),
            machine=str(row.machine_id),
        )
        for row in rows
        if _is_live(row) and _beating(row, now=now)
    ]


__all__ = [
    "CHAT_FOLDER_SUBTYPE",
    "DEFAULT_SYNC_INTERVAL",
    "SERVED_HEARTBEATS",
    "STALE_INTERVALS",
    "HeldLease",
    "LeaseFacet",
    "is_stale",
    "lease_facet",
    "lease_facets",
    "live_holders_under",
]
