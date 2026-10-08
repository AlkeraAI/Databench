"""The in-flight plane: what a leased subtree is doing between two checkpoints.

A lease hands one machine the writes on a folder's subtree; the drive keeps the
last checkpoint and everyone else reads that, so a file an agent is writing
does not exist on the drive until it lands. Here the holder reports what it is
doing per node (writing, uploading, too big to send, postponed) and every
reader under the mount is told, without the bytes moving. The same table
carries the difference the other way: a write admitted into the subtree from
the web is committed on the drive but not yet on the machine, so it is recorded
here until the holder applies it.

Three rules make it safe:

* **The fence comes first.** Every write asserts the epoch and instance the
  lease row names *before* it touches a row, so a superseded machine cannot
  resurrect its view of the tree, and its refused batch leaves no trace (no
  row, no sequence bump, no announcement).
* **A fenced request is confined to what it holds.** An entry naming a node
  outside the lease's subtree refuses the *whole* batch rather than the one
  entry, so a holder cannot learn, one probe at a time, which ids exist outside
  the folder it was given.
* **The plane is bounded.** Past the configured ceiling a batch is refused, so
  a machine that never drains cannot make the drive hold an unbounded list.

``live_seq`` on the lease row is the version every change here bumps: a client
that has seen a lower number knows it is behind without diffing anything, and
the announcement carries that number and the leased node's id and nothing else.

**The lease row is locked before its entries, always, and at update strength
from the first statement.** Every writer here ends by bumping the lease row, so
every writer begins by locking it: the holder's batch through its fence, a
clear through an explicit lock on the leases its rows belong to, an inbound
record through the bump itself. A clear that deleted its rows first and bumped
the lease second would invert that order and deadlock against uploads landing
under the lease; and a fence that took the row ``FOR SHARE`` only to update it
later would deadlock two batches, because neither share can be upgraded while
the other is held.

**And the lease row is updated once per transaction.** Postgres re-checks a
row's foreign keys on the SECOND update of that row inside one transaction,
and the check takes ``FOR KEY SHARE`` on ``file_nodes`` for the leased folder:
a lock on the folder taken after the lease row, the reverse of every writer's
order (the folder at update strength as its gate, then the lease row). Stamping
synced and bumping the sequence in two statements would queue the second on the
folder the holder's own tree report holds while that report queues on the lease
row, a ``40P01`` deadlock. So the stamp rides the bump, and an empty batch
stamps in the one statement that reads the sequence back. The same rule is why
the heartbeat is one statement (``alkera_core.files.leases``).

**The plane's rows take key shares on the nodes they name, after the lease
row.** Inserting an entry takes ``FOR KEY SHARE`` on ``file_nodes`` for the
reported node, a row every writer under the lease takes before the lease row,
so this is a lock the fixed order cannot place. It is admitted instead of
ordered: the node locks writers take are no-key locks
(``FilesRepo.lock_node``), which a key share never queues behind, so the
holder's first report of a file its own move or push already holds lands beside
that writer instead of deadlocking against it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal

from sqlalchemy import text

from alkera_core.authz.principal import ActingContext
from alkera_core.config import settings
from alkera_core.files.freshness import current_sql
from alkera_core.files.history import (
    LEASE_CHANGED_INBOUND,
    LEASE_CHANGED_REPORT,
    emit_lease_changed,
)
from alkera_core.files.ids import NodeId
from alkera_core.files.leases import (
    LeaseConflict,
    LeaseLock,
    assert_lease_epoch,
    holder_identity,
)
from alkera_core.files.repo import SUBTREE_DEPTH_BIND, FilesRepo, subtree_sql
from alkera_core.models.files.leases import LIVE_ENTRY_STATES
from alkera_core.models.files.tree import FileNode

#: What a node is doing, as the column stores it.
LiveState = Literal[
    "writing",
    "uploading",
    "on_box",
    "deferred",
    "inbound",
    "inbound_delete",
    "inbound_rename",
]
#: The two words a holder sends that are not states a node sits in: the
#: difference this node described is gone, either because the holder applied it
#: or because the holder's own copy won. Both mean "delete the row", which is
#: why the column's CHECK must never learn them.
SETTLED_STATES: Final[frozenset[str]] = frozenset({"applied", "superseded"})
#: The kinds of change the drive records for the holder to apply.
InboundKind = Literal["inbound", "inbound_delete", "inbound_rename"]

#: Why a row was cleared. It never reaches a client — the sequence bump is what
#: a client reads — but it names the caller in the log and keeps the two clear
#: paths (bytes landed, holder drained) from being written as one untraceable
#: statement.
ClearReason = Literal["saved", "applied", "released"]

#: Nanoseconds per second, for the mtime the machine reports in wall time and
#: the column stores as an integer.
_NS: Final = 1_000_000_000


@dataclass(frozen=True, slots=True)
class LiveReport:
    """One node's state as the machine holding the lease reports it."""

    node_id: NodeId
    #: A stored state, or one of :data:`SETTLED_STATES` to clear the node.
    state: str
    #: What the machine says the file is becoming, while the drive still holds
    #: the previous version.
    box_size: int | None = None
    box_mtime: datetime | None = None
    #: The conflicted copy the holder's displaced bytes were filed under, on
    #: the ``applied`` report of an inbound write its disk had diverged from.
    #: The drive already holds the copy and its conflict row; this is the
    #: holder's receipt, carried for the report's reader and not stored.
    displaced: str | None = None


@dataclass(frozen=True, slots=True)
class InboundEntry:
    """A change the drive accepted that the holder has yet to apply locally."""

    node_id: NodeId
    state: str
    seq: int


@dataclass(frozen=True, slots=True)
class LiveFacet:
    """What ONE node is doing right now, for a reader who is not the holder."""

    state: str
    box_size: int | None = None
    box_mtime: datetime | None = None
    updated_at: datetime | None = None
    seq: int = 0


@dataclass(frozen=True, slots=True)
class LivePage:
    """The plane's answer for one page of items.

    Two mappings rather than one because they are keyed differently and both
    are needed on every row: ``facets`` is per *node* — only the rows that are
    themselves in flight have one — while ``pending`` is per *lease* and is
    rendered on every row under the mount, including the ones that are idle.
    Returning them together is what keeps the whole answer one statement.
    """

    facets: Mapping[uuid.UUID, LiveFacet] = field(default_factory=dict)
    pending: Mapping[uuid.UUID, int] = field(default_factory=dict)
    #: Per lease: how many files under it still have bytes on their way to
    #: the drive (``behind`` or ``unlanded``, :mod:`alkera_core.files.freshness`).
    landing: Mapping[uuid.UUID, int] = field(default_factory=dict)


def _mismatch() -> LeaseConflict:
    return LeaseConflict("files.lease_mismatch", "that node is not under this lease")


def _too_many(cap: int) -> LeaseConflict:
    return LeaseConflict("files.live_too_many", f"the lease already has {cap} nodes in flight")


def _mtime_ns(value: datetime | None) -> int | None:
    return None if value is None else int(value.timestamp() * _NS)


def _mtime(value: int | None) -> datetime | None:
    if value is None:
        return None
    from datetime import UTC

    return datetime.fromtimestamp(value / _NS, tz=UTC)


#: Every node at or under each lease in ``leases``, walked down ``parent_id``
#: with the columns the landing count reads carried along. The walk is the
#: point: under the Files role the tenant policy is FORCE'd, and PostgreSQL
#: will not run a non-leakproof operator such as ltree containment ahead of it,
#: so a ``path_ids <@`` join could only filter — every file in the org compared
#: against every lease on the page. ``parent_id = id`` is a leakproof equality
#: the parent index serves, so the walk costs the leased subtrees and nothing
#: else. A file has no children, so the walk does not descend from one.
_UNDER_LEASES = (
    "  SELECT l.node_id AS lease_node_id, n.id, n.kind, n.trashed_at, "
    "         n.head_version_id, n.holder_size, n.holder_hash, n.holder_mtime_ns, n.mtime_ns "
    "  FROM leases l JOIN file_nodes n ON n.id = l.node_id AND n.org_team_id = :org "
    "  UNION ALL "
    "  SELECT u.lease_node_id, n.id, n.kind, n.trashed_at, "
    "         n.head_version_id, n.holder_size, n.holder_hash, n.holder_mtime_ns, n.mtime_ns "
    "  FROM under u JOIN file_nodes n ON n.parent_id = u.id AND n.org_team_id = :org "
    "  WHERE u.kind <> 'file'"
)


class LiveEntriesService:
    """Reads and writes the in-flight plane of one org's leases."""

    def __init__(
        self, repo: FilesRepo, ctx: ActingContext, *, verified_machine_id: str | None = None
    ) -> None:
        self._repo = repo
        self._ctx = ctx
        #: Who this caller is to the fence, derived once from the credential
        #: and the machine check — the same derivation the lease service and
        #: every fenced write use, so the plane cannot admit a reporter the
        #: write side would refuse.
        self._identity = holder_identity(ctx, verified_machine_id=verified_machine_id)

    @property
    def _org(self) -> uuid.UUID:
        return self._repo.scope.org_team_id

    async def _fence(
        self, lease_node_id: NodeId, epoch: int, instance_id: str, *, lock: LeaseLock
    ) -> None:
        """The plane's fence: this epoch, this instance, and this caller.

        The epoch and the instance say which grant the report belongs to; who
        is reporting is what says the grant is theirs, and both of the first
        two are derivable from values the product serves.

        ``lock`` is the strength the lease row is then held at: a batch goes on
        to update the row, so it fences at update strength; a drain only reads
        under it.
        """
        await assert_lease_epoch(
            self._repo, lease_node_id, epoch, instance_id, holder=self._identity, lock=lock
        )

    async def upsert(
        self,
        lease_node_id: NodeId,
        *,
        epoch: int,
        instance_id: str,
        entries: Sequence[LiveReport],
    ) -> int:
        """Apply the holder's batch and return the lease's new ``live_seq``.

        The order is the contract: fence, then confinement, then the ceiling,
        then the lease row's one UPDATE — the synced stamp and the sequence
        bump together — then the rows, so every refusal happens before anything
        is written, and a refused batch leaves no row, no bump, no stamp and no
        announcement. The fence locks the lease row at update strength, so that
        UPDATE is the row's own lock and not an upgrade; and it is the row's
        ONLY update in this transaction, because a second one re-checks the
        row's key to the leased folder and takes that folder after the row —
        the inversion the module docstring describes.
        """
        await self._fence(lease_node_id, epoch, instance_id, lock="no key update")
        # A batch that passed the fence is the holder's plane speaking, so the
        # lease has synced — an empty batch included, which is exactly what a
        # holder whose agent has written nothing for a while sends to say so.
        # Without this a chat that was live and idle read as a holder that had
        # stopped syncing after a minute, and every reader was told the copy
        # on screen was behind a machine that was, in fact, right there.
        if not entries:
            return await self._stamp_synced(lease_node_id)

        settled = [e for e in entries if e.state in SETTLED_STATES]
        landing = [e for e in entries if e.state not in SETTLED_STATES]
        for entry in landing:
            if entry.state not in LIVE_ENTRY_STATES:
                raise ValueError(f"unknown live state {entry.state!r}")

        node_ids = [uuid.UUID(str(e.node_id)) for e in entries]
        await self._assert_under(lease_node_id, node_ids)
        if landing:
            await self._assert_room(lease_node_id, [uuid.UUID(str(e.node_id)) for e in landing])

        seq = await self._bump(lease_node_id, synced=True)
        if settled:
            await self._delete(lease_node_id, [uuid.UUID(str(e.node_id)) for e in settled])
        for entry in landing:
            await self._write(lease_node_id, entry, epoch=epoch, seq=seq)
        await self._announce(lease_node_id, seq, reason=LEASE_CHANGED_REPORT)
        return seq

    async def clear(self, node_ids: Iterable[NodeId], *, reason: ClearReason) -> int:
        """Forget what these nodes were doing — the difference is gone.

        Called by every write path that lands bytes, so the overwhelmingly
        common case is that there was no row: a clear that removed nothing
        bumps no sequence and announces nothing, because nothing changed for
        anyone watching.

        Parent first: the leases the rows belong to are looked up and locked —
        in id order, so two clears over several leases cannot cross — and only
        then are the rows deleted and the sequences bumped. A write landing
        under a lease already holds its row at this strength from the fence,
        so for it the lock is free; for any other caller it is what keeps the
        delete-then-bump from meeting the holder's batch coming the other way.
        """
        wanted = [uuid.UUID(str(n)) for n in node_ids]
        if not wanted:
            return 0
        leases = sorted(
            (
                await self._repo.session.execute(
                    text(
                        "SELECT DISTINCT lease_node_id FROM file_lease_live_entries "
                        "WHERE org_team_id = :org AND node_id = ANY(:nodes)"
                    ),
                    {"org": self._org, "nodes": wanted},
                )
            ).scalars()
        )
        if not leases:
            return 0
        await self._repo.session.execute(
            text(
                "SELECT node_id FROM file_leases "
                "WHERE org_team_id = :org AND node_id = ANY(:leases) "
                "ORDER BY node_id FOR NO KEY UPDATE"
            ),
            {"org": self._org, "leases": leases},
        )
        rows = (
            await self._repo.session.execute(
                text(
                    "DELETE FROM file_lease_live_entries "
                    "WHERE org_team_id = :org AND node_id = ANY(:nodes) "
                    "AND lease_node_id = ANY(:leases) "
                    "RETURNING lease_node_id"
                ),
                {"org": self._org, "nodes": wanted, "leases": leases},
            )
        ).all()
        if not rows:
            return 0
        for lease_node_id in sorted({row.lease_node_id for row in rows}):
            seq = await self._bump(NodeId(lease_node_id))
            await self._announce(NodeId(lease_node_id), seq)
        return len(rows)

    async def accept_inbound(self, node: FileNode, *, kind: InboundKind) -> None:
        """Record a write the drive admitted that the holder has yet to apply.

        Idempotent per node: two writes to one file before the holder drains
        leave it one thing to do, and the later kind is the one that describes
        the difference the holder will actually find.
        """
        lease = (
            await self._repo.session.execute(
                text(
                    "SELECT l.node_id FROM file_leases l "  # noqa: S608
                    "JOIN file_nodes n ON n.id = l.node_id "
                    "WHERE l.org_team_id = :org "
                    f"AND {subtree_sql('CAST(:path AS ltree)', 'n.path_ids')} "
                    "AND l.released_at IS NULL AND l.expires_at > now() "
                    "ORDER BY n.depth DESC LIMIT 1"
                ),
                {"org": self._org, "path": node.path_ids, **SUBTREE_DEPTH_BIND},
            )
        ).first()
        if lease is None:
            return
        lease_node_id = NodeId(lease.node_id)
        seq = await self._bump(lease_node_id)
        await self._write(
            lease_node_id,
            LiveReport(node_id=NodeId(node.id), state=kind),
            epoch=await self._epoch_of(lease_node_id),
            seq=seq,
        )
        await self._announce(lease_node_id, seq, reason=LEASE_CHANGED_INBOUND)

    async def inbound_for_holder(
        self, lease_node_id: NodeId, *, epoch: int, instance_id: str
    ) -> list[InboundEntry]:
        """What the holder still has to apply, oldest first.

        Fenced like a write: a machine that was superseded reads as a stranger,
        so it cannot drain — and therefore cannot clear — work that now belongs
        to whoever holds the lease.
        """
        await self._fence(lease_node_id, epoch, instance_id, lock="share")
        rows = (
            await self._repo.session.execute(
                text(
                    "SELECT node_id, state, seq FROM file_lease_live_entries "
                    "WHERE org_team_id = :org AND lease_node_id = :lease "
                    "AND state IN ('inbound', 'inbound_delete', 'inbound_rename') "
                    "ORDER BY seq, node_id"
                ),
                {"org": self._org, "lease": lease_node_id},
            )
        ).all()
        return [
            InboundEntry(node_id=NodeId(row.node_id), state=row.state, seq=row.seq) for row in rows
        ]

    async def facets_for_page(
        self,
        row_ids: Sequence[uuid.UUID],
        *,
        lease_node_ids: Sequence[uuid.UUID],
        now: datetime,
    ) -> LivePage:
        """The plane, for one page of rows, in ONE statement.

        ``lease_node_ids`` are the leases the page's rows are already known to
        be under — the read that found them has the ids in hand — so the count
        is computed over those leases rather than over every entry in the org.
        The leases are re-checked live here rather than trusted: the facet is a
        statement about *now*, and a lease whose TTL has passed stops being one
        the instant it does, with no reaper in between.

        The same statement counts, per lease, the files under it whose bytes
        are still on their way -- the holder reported them and the drive holds
        an older version or none -- which is what a folder's "N landing" reads.
        """
        if not lease_node_ids:
            return LivePage()
        rows = (
            await self._repo.session.execute(
                text(
                    "WITH RECURSIVE leases AS ("  # noqa: S608 - interpolates the builders' own text
                    "  SELECT l.node_id FROM file_leases l "
                    "  JOIN file_nodes ln ON ln.id = l.node_id AND ln.org_team_id = :org "
                    "  WHERE l.org_team_id = :org AND l.node_id = ANY(:leases) "
                    "  AND l.released_at IS NULL AND l.expires_at > :now"
                    "), live AS ("
                    "  SELECT e.lease_node_id, e.node_id, e.state, e.box_size, "
                    "         e.box_mtime_ns, e.updated_at, e.seq "
                    "  FROM file_lease_live_entries e "
                    "  JOIN leases l ON l.node_id = e.lease_node_id "
                    "  WHERE e.org_team_id = :org AND e.node_id = ANY(:rows)"
                    "), counts AS ("
                    "  SELECT e.lease_node_id, count(*) AS pending "
                    "  FROM file_lease_live_entries e "
                    "  JOIN leases l ON l.node_id = e.lease_node_id "
                    "  WHERE e.org_team_id = :org "
                    "  GROUP BY e.lease_node_id"
                    f"), under AS ({_UNDER_LEASES}"
                    "), landing AS ("
                    "  SELECT n.lease_node_id, count(*) AS landing "
                    "  FROM under n LEFT JOIN file_versions hv ON hv.id = n.head_version_id "
                    "  AND hv.org_team_id = :org "
                    "  WHERE n.kind = 'file' AND n.trashed_at IS NULL "
                    f"  AND n.holder_size IS NOT NULL AND NOT {current_sql('n', 'hv')} "
                    "  GROUP BY n.lease_node_id"
                    ") "
                    "SELECT l.node_id AS lease_node_id, coalesce(c.pending, 0) AS pending, "
                    "       coalesce(g.landing, 0) AS landing, f.node_id, f.state, "
                    "       f.box_size, f.box_mtime_ns, f.updated_at, f.seq "
                    "FROM leases l LEFT JOIN counts c ON c.lease_node_id = l.node_id "
                    "LEFT JOIN landing g ON g.lease_node_id = l.node_id "
                    "LEFT JOIN live f ON f.lease_node_id = l.node_id"
                ),
                {
                    "org": self._org,
                    "leases": list(lease_node_ids),
                    "rows": list(row_ids),
                    "now": now,
                },
            )
        ).all()
        facets: dict[uuid.UUID, LiveFacet] = {}
        pending: dict[uuid.UUID, int] = {}
        landing: dict[uuid.UUID, int] = {}
        for row in rows:
            pending[row.lease_node_id] = row.pending
            landing[row.lease_node_id] = row.landing
            if row.node_id is None:
                continue
            facets[row.node_id] = LiveFacet(
                state=row.state,
                box_size=row.box_size,
                box_mtime=_mtime(row.box_mtime_ns),
                updated_at=row.updated_at,
                seq=row.seq,
            )
        return LivePage(facets=facets, pending=pending, landing=landing)

    async def _stamp_synced(self, lease_node_id: NodeId) -> int:
        """The empty batch's one statement: stamp the lease synced and read its
        sequence back, without moving it — nothing changed for anyone watching.

        The batched beat does NOT come here: its synced word rides the beat's
        own UPDATE (``LeaseService.heartbeat(synced=True)``), for the same
        reason this is one statement and not a stamp followed by a read.
        """
        return int(
            (
                await self._repo.session.execute(
                    text(
                        "UPDATE file_leases SET last_sync_at = now() "
                        "WHERE org_team_id = :org AND node_id = :node RETURNING live_seq"
                    ),
                    {"org": self._org, "node": lease_node_id},
                )
            ).scalar_one()
        )

    async def _epoch_of(self, lease_node_id: NodeId) -> int:
        return int(
            (
                await self._repo.session.execute(
                    text(
                        "SELECT epoch FROM file_leases WHERE org_team_id = :org AND node_id = :node"
                    ),
                    {"org": self._org, "node": lease_node_id},
                )
            ).scalar_one()
        )

    async def _assert_under(self, lease_node_id: NodeId, node_ids: Sequence[uuid.UUID]) -> None:
        """Every named node is the leased node or under it, or nothing happens.

        One statement for the batch: it counts how many of the ids resolve to a
        node whose path is under the lease's, and any shortfall — a foreign id,
        a purged id, an id in another org — is the same refusal, so the batch
        never becomes a probe for which of those it was.
        """
        found = int(
            (
                await self._repo.session.execute(
                    text(
                        "SELECT count(*) FROM file_nodes n "  # noqa: S608
                        "JOIN file_nodes lease ON lease.id = :lease "
                        "AND lease.org_team_id = :org "
                        "WHERE n.org_team_id = :org AND n.id = ANY(:nodes) "
                        f"AND {subtree_sql('n.path_ids', 'lease.path_ids')}"
                    ),
                    {
                        "org": self._org,
                        "lease": lease_node_id,
                        "nodes": list(set(node_ids)),
                        **SUBTREE_DEPTH_BIND,
                    },
                )
            ).scalar_one()
        )
        if found != len(set(node_ids)):
            raise _mismatch()

    async def _assert_room(self, lease_node_id: NodeId, node_ids: Sequence[uuid.UUID]) -> None:
        """Refuse a batch that would push the lease past the pending ceiling.

        What is counted is how many *nodes* would be in flight afterwards, not
        how many reports arrived: a holder sitting at the ceiling still has to
        be able to say "that one finished", or the plane wedges at exactly the
        moment it matters.
        """
        cap = settings.files_live_max_pending_entries
        after = int(
            (
                await self._repo.session.execute(
                    text(
                        "SELECT count(*) FROM ("
                        "  SELECT node_id FROM file_lease_live_entries "
                        "  WHERE org_team_id = :org AND lease_node_id = :lease "
                        "  UNION SELECT unnest(CAST(:nodes AS uuid[]))"
                        ") AS in_flight"
                    ),
                    {"org": self._org, "lease": lease_node_id, "nodes": list(set(node_ids))},
                )
            ).scalar_one()
        )
        if after > cap:
            raise _too_many(cap)

    async def _bump(self, lease_node_id: NodeId, *, synced: bool = False) -> int:
        """Advance the lease's sequence — and, for the holder's own batch, stamp
        it synced in the same statement.

        One UPDATE however much it has to say: this is the lease row's only
        update in the transaction that holds nothing but the row (the module
        docstring says why a second one takes the leased folder after it).
        ``synced`` is the holder's word that its plane is running; a clear or
        an inbound record says nothing about the plane and leaves the stamp.
        """
        return int(
            (
                await self._repo.session.execute(
                    text(
                        "UPDATE file_leases SET live_seq = live_seq + 1, "
                        "last_sync_at = CASE WHEN CAST(:synced AS boolean) THEN now() "
                        "ELSE last_sync_at END "
                        "WHERE org_team_id = :org AND node_id = :node RETURNING live_seq"
                    ),
                    {"org": self._org, "node": lease_node_id, "synced": synced},
                )
            ).scalar_one()
        )

    async def _write(
        self, lease_node_id: NodeId, entry: LiveReport, *, epoch: int, seq: int
    ) -> None:
        await self._repo.session.execute(
            text(
                "INSERT INTO file_lease_live_entries "
                "(lease_node_id, node_id, org_team_id, state, lease_epoch, box_size, "
                " box_mtime_ns, seq, updated_at) "
                "VALUES (:lease, :node, :org, :state, :epoch, :size, :mtime, :seq, now()) "
                "ON CONFLICT (lease_node_id, node_id) DO UPDATE SET "
                "state = EXCLUDED.state, lease_epoch = EXCLUDED.lease_epoch, "
                "box_size = EXCLUDED.box_size, box_mtime_ns = EXCLUDED.box_mtime_ns, "
                "seq = EXCLUDED.seq, updated_at = now()"
            ),
            {
                "lease": lease_node_id,
                "node": uuid.UUID(str(entry.node_id)),
                "org": self._org,
                "state": entry.state,
                "epoch": epoch,
                "size": entry.box_size,
                "mtime": _mtime_ns(entry.box_mtime),
                "seq": seq,
            },
        )

    async def _delete(self, lease_node_id: NodeId, node_ids: Sequence[uuid.UUID]) -> None:
        await self._repo.session.execute(
            text(
                "DELETE FROM file_lease_live_entries WHERE org_team_id = :org "
                "AND lease_node_id = :lease AND node_id = ANY(:nodes)"
            ),
            {"org": self._org, "lease": lease_node_id, "nodes": list(node_ids)},
        )

    async def _announce(
        self, lease_node_id: NodeId, seq: int, *, reason: str | None = None
    ) -> None:
        drive_id = (
            await self._repo.session.execute(
                text("SELECT drive_id FROM file_nodes WHERE org_team_id = :org AND id = :node"),
                {"org": self._org, "node": lease_node_id},
            )
        ).scalar_one()
        await emit_lease_changed(
            self._repo,
            self._ctx,
            lease_node_id=lease_node_id,
            drive_id=drive_id,
            live_seq=seq,
            reason=reason,
        )


__all__: list[str] = [
    "SETTLED_STATES",
    "ClearReason",
    "InboundEntry",
    "InboundKind",
    "LiveEntriesService",
    "LiveFacet",
    "LivePage",
    "LiveReport",
    "LiveState",
]
