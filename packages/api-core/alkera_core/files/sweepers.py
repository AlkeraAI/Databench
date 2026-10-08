"""The reconciliation sweepers — one place where everything with a deadline dies.

Every row and every byte in Files has an owner, a deadline and a sweeper. This
module holds one class per row of the reconciliation ledger, each one
idempotent (a second run over the same state clears nothing) and resumable (a
sweeper killed mid-pass restarts from the cursor it returned, never from the
beginning). `run_janitor` runs them in a fixed order with a per-sweeper cursor,
which is what the `files.janitor` Temporal schedule wraps.

Deadlines are compared inside the statement against the instant the caller
passes, which the worker reads from Postgres `now()`: no sweeper decides
"expired" against its own host clock, so a skewed worker cannot delete early.
A sweeper whose dependency is not wired in this deployment reports
`skipped=True` rather than pretending it swept — an unwired sweeper is a
visible gap in the report, never a silent zero.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, ClassVar, Final, Literal, Protocol, runtime_checkable

from sqlalchemy import text

from alkera_core.authz.principal import ActingContext
from alkera_core.config import get_settings
from alkera_core.db.tenant_session import stepped_out
from alkera_core.files import acl, history, stats
from alkera_core.files.checkpoints import Checkpoints, NoopCheckpoints
from alkera_core.files.clock import Clock, SystemClock
from alkera_core.files.errors import InvalidRequest, NotFound
from alkera_core.files.gc import SweepPlan, SweepResult
from alkera_core.files.ids import DomainId, DriveId, NodeId, OperationId, OrgScope
from alkera_core.files.leases import DEFAULT_GRANT_DELAY
from alkera_core.files.repo import SUBTREE_DEPTH_BIND, FilesRepo, subtree_sql
from alkera_core.files.store.keys import DOMAIN_PREFIX
from alkera_core.files.trash import TRASH_WINDOW, trash_left_behind_sql
from alkera_core.models.files.ops import FileOp

#: How long an upload session's staged bytes outlive the session's own TTL.
INCOMING_GRACE = timedelta(days=2)
#: The store-side multipart deadline (the bucket rule is the safety net).
MULTIPART_TTL = timedelta(days=7)
#: A content-URL nonce lives five minutes; the row is kept an hour past expiry
#: so a redemption arriving on a skewed clock still sees `used_at`.
NONCE_RETENTION = timedelta(hours=1)
#: Operations, their inverses and history all age out at the same 90 days.
OPERATION_RETENTION = timedelta(days=90)
HISTORY_HOT = timedelta(days=90)
#: A resolved conflict or a resolved quarantine row is evidence for a month.
RESOLVED_RETENTION = timedelta(days=30)
#: Unreferenced interned ACL rows are swept weekly, not on every pass.
ACL_SWEEP_INTERVAL = timedelta(days=7)
#: A node flagged for a background rewrite or a large move is stale after this
#: long with no live operation behind it.
FLAG_STALE_AFTER = timedelta(minutes=10)

#: The history kinds compaction keeps whole. Renames, moves and attribute
#: diffs are folded into one summary row per node instead.
HISTORY_KEPT_KINDS: tuple[str, ...] = ("create", "trash", "restore", "acl")
HISTORY_COMPACTED_KINDS: tuple[str, ...] = ("rename", "move", "attrs")
#: An upload session in one of these states no longer owns its staged bytes.
TERMINAL_SESSION_STATES: tuple[str, ...] = ("done", "aborted", "expired")

#: How long a version written by a live write stays individually addressable.
#: Inside the window an agent's per-keystroke saves are still a history a
#: person can scrub; past it they are noise, and one landing per generation is
#: what anybody ever asks for.
LIVE_VERSION_HOT = timedelta(minutes=10)
#: How many non-head versions one generation of live writes keeps once it is
#: cold. The head is kept on top of this, always.
LIVE_VERSIONS_KEPT: Final = 1
#: How long a co-edited document's write back stays individually addressable.
#: It is the window a live session remembers the source versions it matched
#: in, so nothing a session could still merge from goes cold inside it.
WRITE_BACK_HOT = timedelta(minutes=15)
#: Past the hot window, one write back is kept per slot of this length within
#: a run (on top of the run's first and last), so a long session still reads
#: as a history a person can step back through at this grain.
WRITE_BACK_KEEP_EVERY = timedelta(minutes=10)
#: How many of a file's latest write backs in a live session's current epoch
#: are never collapsed: at least as many as the session looks back through
#: when it merges an outside change (the write backs it made and may not have
#: recorded), so a collapse never takes a base the merge could pick.
WRITE_BACK_POSITIONS_KEPT: Final = 512
#: The reason a file the holder reported, and whose bytes never landed, is
#: trashed once its lease has been gone for the grace. The trash entry names
#: the machine the bytes were left on.
REASON_LEFT_ON_MACHINE: Final = "left_on_machine"
#: How many folders one lease's grace sweep announces by name. Past it the
#: sweep announces none and marks the lease frame ``subtree``, as a tree
#: report does.
UNSYNCED_MAX_NAMED_FOLDERS: Final = 32

DEFAULT_BUDGET = 1_000

#: What the store can say about a domain's prefix. `ours` and `foreign` are
#: both ANSWERS and are recorded; `unknown` is a store that would not say, and
#: leaves the question open for the next pass.
MarkerVerdict = Literal["ours", "foreign", "unknown"]


@dataclass(frozen=True)
class SweepOutcome:
    """What one sweeper did in one pass.

    `cursor` is `None` when the pass reached the end of its work; anything else
    is the token the next call must be handed to resume — which is also how the
    janitor knows a budgeted sweeper has more to do.
    """

    name: str
    swept: int = 0
    scanned: int = 0
    cursor: str | None = None
    skipped: bool = False
    reason: str | None = None


@runtime_checkable
class IncomingAdmin(Protocol):
    """The janitor's admin handle over a domain's `incoming/` prefix."""

    async def list_incoming(
        self, *, after: str | None, limit: int
    ) -> tuple[Sequence[str], str | None]:
        """One page of staged keys, and the token for the next page."""
        ...

    async def written_at(self, key: str) -> datetime | None:
        """When the object was written, or `None` when the store cannot say."""
        ...

    async def delete(self, key: str) -> None:
        """Remove one staged object."""
        ...


@runtime_checkable
class MultipartAdmin(Protocol):
    """A driver that can list and abort its own incomplete multipart uploads."""

    async def list_incomplete(self) -> Sequence[tuple[str, str, datetime]]:
        """`(upload_id, key, initiated_at)` for every unfinished upload."""
        ...

    async def abort(self, upload_id: str, key: str) -> None:
        """Abort one multipart upload."""
        ...


@runtime_checkable
class ObjectAdmin(IncomingAdmin, MultipartAdmin, Protocol):
    """Both admin surfaces on one handle, which is how a driver exposes them."""


@dataclass(frozen=True)
class DomainBoundAdmin:
    """One domain's slice of a bucket-rooted admin handle, in the sweepers' namespace.

    The handle a store factory's ``admin()`` returns is rooted *above* every
    domain: it answers ``domains/<uuid>/incoming/<session>/...`` and it answers
    for every domain in the bucket at once. The sweepers read the session id
    out of ``incoming/<session>/...`` and ask an org-scoped repo which of those
    sessions are still live, so handing the raw handle to :class:`SweepDeps`
    makes every staged object read session-less: the live-session guard never
    matches, and the bytes of an upload still in progress are released the
    moment they pass the grace. This binds the handle to one domain -- a key
    belonging to another domain is dropped, the domain prefix is stripped on
    the way out and restored on the way back in -- so a page round-trips into
    ``written_at``/``delete``/``abort`` and the keys the sweepers parse are the
    relative ones they were written for.

    The page cursor is *not* translated: it is the inner handle's own resume
    token, the sweepers never parse it, and a page may well end on a key this
    domain does not own.
    """

    inner: ObjectAdmin
    domain_id: DomainId

    @property
    def prefix(self) -> str:
        """What every key of this domain starts with, in the admin's namespace."""
        return f"{DOMAIN_PREFIX}{self.domain_id}/"

    async def list_incoming(
        self, *, after: str | None = None, limit: int = DEFAULT_BUDGET
    ) -> tuple[Sequence[str], str | None]:
        keys, next_after = await self.inner.list_incoming(after=after, limit=limit)
        prefix = self.prefix
        return [key[len(prefix) :] for key in keys if key.startswith(prefix)], next_after

    async def written_at(self, key: str) -> datetime | None:
        return await self.inner.written_at(f"{self.prefix}{key}")

    async def delete(self, key: str) -> None:
        await self.inner.delete(f"{self.prefix}{key}")

    async def list_incomplete(self) -> Sequence[tuple[str, str, datetime]]:
        prefix = self.prefix
        return [
            (upload_id, key[len(prefix) :], initiated_at)
            for upload_id, key, initiated_at in await self.inner.list_incomplete()
            if key.startswith(prefix)
        ]

    async def abort(self, upload_id: str, key: str) -> None:
        await self.inner.abort(upload_id, f"{self.prefix}{key}")


@runtime_checkable
class ReachabilityJanitor(Protocol):
    """The mark-and-sweep that owns the `objects/` row of the ledger.

    Declared as a protocol rather than taken as `gc.Janitor` so a deployment
    can hand the sweeper a differently-budgeted or differently-held sweeper
    without this module learning about it.
    """

    async def claim_shard(self, repo: FilesRepo, shard: int) -> bool:
        """Take the shard's lease, or report that somebody else holds it."""
        ...

    async def release_shard(self, repo: FilesRepo, shard: int) -> None:
        """Give the shard back, if this sweeper still holds it."""
        ...

    async def sweep(
        self,
        domain_id: DomainId,
        *,
        org: OrgScope,
        shard: int = 0,
        dry_run: bool,
        budget_bytes: int | None = None,
    ) -> SweepPlan | SweepResult:
        """Compute reachability and move what nothing references."""
        ...


@dataclass(frozen=True)
class ReachabilityTarget:
    """Which domain the reachability sweep runs over, and who runs it.

    The domain is bound here rather than derived in the sweeper because the
    domain-to-org map is platform data the org-scoped repo cannot read; the
    deployment that iterates orgs already knows it.
    """

    janitor: ReachabilityJanitor
    domain_id: DomainId
    shard: int = 0
    budget_bytes: int | None = None


@dataclass
class SweepDeps:
    """Everything the sweepers reach for, injected rather than imported.

    The delegating sweepers take the bound method of the service that owns the
    behaviour (`gc.Janitor.expire_deleted`, `uploads.UploadService.sweep_expired`,
    …) so this module never re-implements a state machine that already exists.
    A dependency left `None` makes its sweeper report `skipped`.
    """

    repo: FilesRepo
    ctx: ActingContext
    clock: Clock = field(default_factory=SystemClock)
    checkpoints: Checkpoints = field(default_factory=NoopCheckpoints)
    incoming: IncomingAdmin | None = None
    multipart: MultipartAdmin | None = None
    expire_deleted: Callable[[datetime], Awaitable[Sequence[str]]] | None = None
    sweep_expired_sessions: Callable[[datetime], Awaitable[int]] | None = None
    watchdog: Callable[[datetime], Awaitable[Sequence[Any]]] | None = None
    reap_leases: Callable[[datetime], Awaitable[int]] | None = None
    lease_grant_delay: timedelta = DEFAULT_GRANT_DELAY
    unsynced_grace: timedelta = field(
        default_factory=lambda: timedelta(seconds=get_settings().files_unsynced_grace_seconds)
    )
    """How long a released or lapsed lease's facet-only rows read "unsynced"
    before the reaper trashes them. Inside it the same machine coming back
    reconciles them; past it nobody will."""
    """How long a reaped folder stays ungrantable. The delay is what stops a
    new holder taking the folder while the old holder's box is still finishing
    the writes it started before the network cut."""
    aggregate_stats: Callable[[], Awaitable[int]] | None = None
    reachability: ReachabilityTarget | None = None
    """The domain-bound mark-and-sweep. Left `None`, unreferenced objects are
    never released and `ReachabilitySweep` says so instead of reporting zero."""
    purge_trash: Callable[[NodeId], Awaitable[None]] | None = None
    stamp_domain: Callable[[uuid.UUID, datetime], Awaitable[MarkerVerdict]] | None = None
    """Write a domain prefix's ownership marker, dated as the caller says the
    domain was made, and report whose the prefix is afterwards. It is a store
    write, so it is injected rather than done here: this module holds no store,
    no bucket and no deployment id."""


class Sweeper:
    """One ledger row's reaper.

    Subclasses implement `run`; the name is the metric label, the report key
    and the cursor key, so it is spelled once here and nowhere else.
    """

    name: ClassVar[str] = ""

    def __init__(self, deps: SweepDeps) -> None:
        self._deps = deps

    @property
    def repo(self) -> FilesRepo:
        return self._deps.repo

    async def run(
        self, now: datetime, *, cursor: str | None = None, budget: int = DEFAULT_BUDGET
    ) -> SweepOutcome:  # pragma: no cover - abstract
        raise NotImplementedError

    # -- helpers ----------------------------------------------------------

    def _skip(self, reason: str) -> SweepOutcome:
        return SweepOutcome(name=self.name, skipped=True, reason=reason)

    async def _reach(self, point: str) -> None:
        await self._deps.checkpoints.reach(f"sweepers.{self.name}.{point}")

    async def _delete(self, statement: str, params: dict[str, Any], budget: int) -> SweepOutcome:
        """Run one budgeted DELETE and report whether more work remains.

        The statement selects its victims by `ctid` under a `LIMIT`, so a pass
        is bounded whatever the table holds, and the outcome carries a cursor
        exactly when the batch filled the budget — the janitor's signal that
        this sweeper is not done.
        """
        async with self.repo.transaction():
            await self._reach("before_delete")
            rows = (
                await self.repo.session.execute(
                    text(statement), {**params, "limit": budget, "org": self.repo.scope.org_team_id}
                )
            ).fetchall()
            await self._reach("after_delete")
        swept = len(rows)
        return SweepOutcome(
            name=self.name, swept=swept, scanned=swept, cursor="more" if swept >= budget else None
        )


class OwnerMarkers(Sweeper):
    """Dedup domains whose prefix does not yet say whose bytes it holds.

    The marker is written when the domain is made, on a request that must not
    wait for the object store — so a store that was unreachable, or slower than
    the stamp's deadline, leaves the statement owed. It is owed, not lost: the
    collector refuses to collect a prefix it cannot see its own name on, so an
    unmarked domain keeps its bytes forever and costs storage until somebody
    says whose they are.

    Every answer the store can give is recorded, and that is what makes the
    pass cheap in the steady state. `ours` sets `owner_marked_at`; `foreign` —
    a prefix another deployment stamped, which the marker being unoverwritable
    means will never become ours — sets `owner_marker_conflict_at`. Both drop
    the domain out of this sweeper's predicate for good. Only a store that
    would not answer leaves a domain owed, and the attempt each pass records on
    those rows — see `run` — is what stops a page of them from being the only
    page a pass ever reaches.

    Idempotent by construction, which is what the first ticks after the column
    landed depend on: every domain that predates it reads as owed, and settling
    one costs a conditional PUT that lands when the prefix is unmarked, or
    fails and is followed by a GET to read whose name is on it. The marker is
    never overwritten either way, so a pass over prefixes this deployment has
    already marked changes not a byte of the store.
    """

    name = "owner_markers"

    async def run(
        self, now: datetime, *, cursor: str | None = None, budget: int = DEFAULT_BUDGET
    ) -> SweepOutcome:
        """Settle one budgeted page of the org's unresolved domains.

        `cursor` is not read and none is handed back: this sweeper's resume
        position is a column. A domain the store would not answer for stays
        owed on purpose, so a pass ordered by anything fixed would re-read the
        same page every tick and never reach the rows behind it. Ordered by
        when each domain was last ASKED, the page the last tick could not
        settle goes to the back and the rest come forward — with no token for
        anybody to persist, feed back, or lose across a worker restart.
        """
        stamp = self._deps.stamp_domain
        if stamp is None:
            return self._skip("no store to write the marker with")
        async with self.repo.transaction():
            owed = (
                await self.repo.session.execute(
                    text(
                        "SELECT id, created_at FROM dedup_domains "
                        "WHERE org_team_id = :org AND owner_marked_at IS NULL "
                        "AND owner_marker_conflict_at IS NULL "
                        # NULLS FIRST: a domain nobody has asked about yet goes
                        # ahead of one this deployment has already tried.
                        "ORDER BY owner_marker_attempted_at ASC NULLS FIRST, created_at, id "
                        "LIMIT :limit"
                    ),
                    {"org": self.repo.scope.org_team_id, "limit": budget},
                )
            ).fetchall()
        if not owed:
            return SweepOutcome(name=self.name)
        await self._reach("after_read")
        settled = 0
        unanswered: list[Any] = []
        for domain_id, created_at in owed:
            verdict = await stamp(uuid.UUID(str(domain_id)), created_at)
            if verdict == "unknown":
                unanswered.append(domain_id)
                continue
            # The marker is dated as the domain was made, but the RECORD is
            # dated now: the row states when this deployment resolved the
            # prefix, which is the only thing that stops the next pass asking
            # the store about it again.
            column = "owner_marked_at" if verdict == "ours" else "owner_marker_conflict_at"
            async with self.repo.transaction():
                await self.repo.session.execute(
                    text(
                        f"UPDATE dedup_domains SET {column} = :now "  # noqa: S608 - two literals
                        "WHERE id = :id AND org_team_id = :org"
                    ),
                    {"now": now, "id": domain_id, "org": self.repo.scope.org_team_id},
                )
            settled += 1
        if unanswered:
            # What makes the next pass start somewhere else. One statement for
            # the whole page, so an unreachable backlog costs one write a tick.
            async with self.repo.transaction():
                await self.repo.session.execute(
                    text(
                        "UPDATE dedup_domains SET owner_marker_attempted_at = :now "
                        "WHERE org_team_id = :org AND id = ANY(:ids)"
                    ),
                    {"now": now, "org": self.repo.scope.org_team_id, "ids": unanswered},
                )
        await self._reach("after_stamp")
        return SweepOutcome(
            name=self.name,
            swept=settled,
            scanned=len(owed),
            # The page filled the budget, so there may be more behind it. Where
            # the next pass picks up is decided by the rows, not by this token.
            cursor="more" if len(owed) >= budget else None,
        )


class IncomingOrphans(Sweeper):
    """Staged upload bytes whose session is terminal or gone.

    The session row is written before the bytes, so an object under
    `incoming/<session>/` always has an owner to ask about; only a session that
    reached a terminal state (or vanished with its row) releases them, and only
    once the object itself is older than the session TTL plus the grace.
    """

    name = "incoming_orphans"

    async def run(
        self, now: datetime, *, cursor: str | None = None, budget: int = DEFAULT_BUDGET
    ) -> SweepOutcome:
        admin = self._deps.incoming
        if admin is None:
            return self._skip("no admin store handle")
        keys, next_after = await admin.list_incoming(after=cursor, limit=budget)
        await self._reach("after_list")
        live = await self._live_sessions(keys)
        deleted = 0
        for key in keys:
            session_id = _session_of(key)
            if session_id is not None and session_id in live:
                continue
            written = await admin.written_at(key)
            if written is None or written > now - INCOMING_GRACE:
                continue
            await admin.delete(key)
            await self._reach("after_object")
            deleted += 1
        return SweepOutcome(
            name=self.name,
            swept=deleted,
            scanned=len(keys),
            cursor=next_after,
        )

    async def _live_sessions(self, keys: Sequence[str]) -> set[uuid.UUID]:
        """Which of the page's sessions still own their bytes."""
        ids = {sid for sid in (_session_of(key) for key in keys) if sid is not None}
        if not ids:
            return set()
        async with self.repo.transaction():
            rows = (
                await self.repo.session.execute(
                    text(
                        "SELECT id FROM file_upload_sessions "
                        "WHERE org_team_id = :org AND id = ANY(:ids) "
                        "AND state <> ALL(:terminal)"
                    ),
                    {
                        "org": self.repo.scope.org_team_id,
                        "ids": list(ids),
                        "terminal": list(TERMINAL_SESSION_STATES),
                    },
                )
            ).fetchall()
        return {uuid.UUID(str(row[0])) for row in rows}


class DeletedExpiry(Sweeper):
    """Hard-delete what the reachability sweep moved under `deleted/`."""

    name = "deleted_expiry"

    async def run(
        self, now: datetime, *, cursor: str | None = None, budget: int = DEFAULT_BUDGET
    ) -> SweepOutcome:
        expire = self._deps.expire_deleted
        if expire is None:
            return self._skip("no gc janitor")
        erased = await expire(now)
        await self._reach("after_expire")
        return SweepOutcome(name=self.name, swept=len(erased), scanned=len(erased))


class StoreMultipartAborts(Sweeper):
    """Abort store-side multipart uploads no live session owns."""

    name = "store_multipart_aborts"

    async def run(
        self, now: datetime, *, cursor: str | None = None, budget: int = DEFAULT_BUDGET
    ) -> SweepOutcome:
        admin = self._deps.multipart
        if admin is None:
            return self._skip("driver lists no multipart uploads")
        pending = list(await admin.list_incomplete())[:budget]
        live = await IncomingOrphans(self._deps)._live_sessions([key for _, key, _ in pending])
        aborted = 0
        for upload_id, key, initiated_at in pending:
            session_id = _session_of(key)
            if session_id is not None and session_id in live:
                continue
            if initiated_at > now - MULTIPART_TTL and session_id is not None:
                continue
            await admin.abort(upload_id, key)
            await self._reach("after_abort")
            aborted += 1
        return SweepOutcome(name=self.name, swept=aborted, scanned=len(pending))


class ExpiredSessions(Sweeper):
    """Expire idle upload sessions and release their quota holds."""

    name = "expired_sessions"

    async def run(
        self, now: datetime, *, cursor: str | None = None, budget: int = DEFAULT_BUDGET
    ) -> SweepOutcome:
        sweep = self._deps.sweep_expired_sessions
        if sweep is None:
            return self._skip("no upload service")
        swept = await sweep(now)
        await self._reach("after_sessions")
        return SweepOutcome(name=self.name, swept=swept, scanned=swept)


class IdempotencyKeys(Sweeper):
    """Replay records past the expiry their scope gave them."""

    name = "idempotency_keys"

    async def run(
        self, now: datetime, *, cursor: str | None = None, budget: int = DEFAULT_BUDGET
    ) -> SweepOutcome:
        return await self._delete(
            "DELETE FROM file_idempotency_keys WHERE ctid IN ("
            "SELECT ctid FROM file_idempotency_keys "
            "WHERE org_team_id = :org AND expires_at <= :now LIMIT :limit"
            ") RETURNING key",
            {"now": now},
            budget,
        )


class ContentGrantNonces(Sweeper):
    """Signed content-URL nonces, an hour past the five minutes they live."""

    name = "content_grant_nonces"

    async def run(
        self, now: datetime, *, cursor: str | None = None, budget: int = DEFAULT_BUDGET
    ) -> SweepOutcome:
        return await self._delete(
            "DELETE FROM file_content_grants WHERE ctid IN ("
            "SELECT ctid FROM file_content_grants "
            "WHERE org_team_id = :org AND expires_at <= :deadline LIMIT :limit"
            ") RETURNING nonce",
            {"deadline": now - NONCE_RETENTION},
            budget,
        )


class PageGrantNonces(Sweeper):
    """Page grants, an hour past the fifteen minutes they live.

    Kept past expiry for the same reason the content nonces are: a request
    arriving on a skewed clock must find the row and be refused by it, rather
    than find nothing and be refused for a reason nobody can audit. A revoked
    grant is swept on its own expiry — revoking closes it, it does not shorten
    the evidence.
    """

    name = "page_grant_nonces"

    async def run(
        self, now: datetime, *, cursor: str | None = None, budget: int = DEFAULT_BUDGET
    ) -> SweepOutcome:
        return await self._delete(
            "DELETE FROM file_page_grants WHERE ctid IN ("
            "SELECT ctid FROM file_page_grants "
            "WHERE org_team_id = :org AND expires_at <= :deadline LIMIT :limit"
            ") RETURNING nonce",
            {"deadline": now - NONCE_RETENTION},
            budget,
        )


class OperationsRetention(Sweeper):
    """Operations past ninety days, unless an undo chain is still open."""

    name = "operations_retention"

    async def run(
        self, now: datetime, *, cursor: str | None = None, budget: int = DEFAULT_BUDGET
    ) -> SweepOutcome:
        return await self._delete(
            "DELETE FROM file_ops WHERE ctid IN ("
            "SELECT ctid FROM file_ops WHERE org_team_id = :org "
            "AND created_at <= :deadline AND state <> 'running' AND state <> 'queued' "
            "AND (undoable_until IS NULL OR undoable_until <= :now) LIMIT :limit"
            ") RETURNING id",
            {"deadline": now - OPERATION_RETENTION, "now": now},
            budget,
        )


class HistoryCompaction(Sweeper):
    """Fold a node's cold renames, moves and attribute diffs into one row.

    Creates, trashes, restores and ACL changes are the audit trail and survive
    whole; the churn kinds collapse to the newest row per node, whose `after`
    records how many were folded into it. Compacting is a rewrite plus a
    delete in one transaction, so a crash never loses the summary.
    """

    name = "history_compaction"

    async def run(
        self, now: datetime, *, cursor: str | None = None, budget: int = DEFAULT_BUDGET
    ) -> SweepOutcome:
        deadline = now - HISTORY_HOT
        async with self.repo.transaction():
            keepers = (
                await self.repo.session.execute(
                    text(
                        "SELECT DISTINCT ON (node_id) node_id, id, seq FROM file_history "
                        "WHERE org_team_id = :org AND at <= :deadline AND kind = ANY(:kinds) "
                        "ORDER BY node_id, seq DESC LIMIT :limit"
                    ),
                    {
                        "org": self.repo.scope.org_team_id,
                        "deadline": deadline,
                        "kinds": list(HISTORY_COMPACTED_KINDS),
                        "limit": budget,
                    },
                )
            ).fetchall()
            await self._reach("after_pick")
            folded = 0
            for node_id, keeper_id, _seq in keepers:
                dropped = (
                    await self.repo.session.execute(
                        text(
                            "DELETE FROM file_history WHERE org_team_id = :org "
                            "AND node_id = :node AND at <= :deadline AND kind = ANY(:kinds) "
                            "AND id <> :keeper RETURNING id"
                        ),
                        {
                            "org": self.repo.scope.org_team_id,
                            "node": node_id,
                            "deadline": deadline,
                            "kinds": list(HISTORY_COMPACTED_KINDS),
                            "keeper": keeper_id,
                        },
                    )
                ).fetchall()
                if not dropped:
                    continue
                await self.repo.session.execute(
                    text(
                        "UPDATE file_history SET after = "
                        "coalesce(after, '{}'::jsonb) || jsonb_build_object("
                        "'compacted', CAST(:count AS int)) WHERE id = :keeper"
                    ),
                    {"count": len(dropped) + 1, "keeper": keeper_id},
                )
                folded += len(dropped)
            await self._reach("after_fold")
        return SweepOutcome(
            name=self.name,
            swept=folded,
            scanned=len(keepers),
            cursor="more" if len(keepers) >= budget else None,
        )


class ConflictsAndQuarantine(Sweeper):
    """Resolved conflicts and resolved quarantine rows, thirty days on."""

    name = "conflicts_and_quarantine"

    async def run(
        self, now: datetime, *, cursor: str | None = None, budget: int = DEFAULT_BUDGET
    ) -> SweepOutcome:
        deadline = now - RESOLVED_RETENTION
        async with self.repo.transaction():
            await self._reach("before_delete")
            conflicts = (
                await self.repo.session.execute(
                    text(
                        "DELETE FROM file_conflicts WHERE ctid IN ("
                        "SELECT ctid FROM file_conflicts WHERE org_team_id = :org "
                        "AND state <> 'open' AND resolved_at IS NOT NULL "
                        "AND resolved_at <= :deadline LIMIT :limit) RETURNING id"
                    ),
                    {
                        "org": self.repo.scope.org_team_id,
                        "deadline": deadline,
                        "limit": budget,
                    },
                )
            ).fetchall()
            quarantined = (
                await self.repo.session.execute(
                    text(
                        "DELETE FROM file_quarantine WHERE ctid IN ("
                        "SELECT ctid FROM file_quarantine WHERE org_team_id = :org "
                        "AND resolved_at IS NOT NULL AND resolved_at <= :deadline "
                        "LIMIT :limit) RETURNING id"
                    ),
                    {
                        "org": self.repo.scope.org_team_id,
                        "deadline": deadline,
                        "limit": budget,
                    },
                )
            ).fetchall()
            await self._reach("after_delete")
        swept = len(conflicts) + len(quarantined)
        return SweepOutcome(name=self.name, swept=swept, scanned=swept)


class UnreferencedAcls(Sweeper):
    """Interned ACL rows nothing points at any more — weekly.

    The cursor carries the instant of the last completed pass, so the weekly
    cadence survives a restart without a table of its own.
    """

    name = "unreferenced_acls"

    async def run(
        self, now: datetime, *, cursor: str | None = None, budget: int = DEFAULT_BUDGET
    ) -> SweepOutcome:
        last = _parse_when(cursor)
        if last is not None and now - last < ACL_SWEEP_INTERVAL:
            return SweepOutcome(name=self.name, cursor=cursor, skipped=True, reason="not due")
        async with self.repo.transaction():
            await self._reach("before_delete")
            rows = (
                await self.repo.session.execute(
                    text(
                        "DELETE FROM file_acls a WHERE a.ctid IN ("
                        "SELECT a2.ctid FROM file_acls a2 WHERE a2.org_team_id = :org "
                        "AND NOT EXISTS (SELECT 1 FROM file_nodes n WHERE "
                        "n.acl_id = a2.id OR n.default_acl_id = a2.id) LIMIT :limit"
                        ") RETURNING a.id"
                    ),
                    {"org": self.repo.scope.org_team_id, "limit": budget},
                )
            ).fetchall()
            await self._reach("after_delete")
        return SweepOutcome(
            name=self.name, swept=len(rows), scanned=len(rows), cursor=now.isoformat()
        )


class TrashPurge(Sweeper):
    """Trashed roots whose grace period has run out."""

    name = "trash_purge"

    async def run(
        self, now: datetime, *, cursor: str | None = None, budget: int = DEFAULT_BUDGET
    ) -> SweepOutcome:
        purge = self._deps.purge_trash
        if purge is None:
            return self._skip("no trash service")
        async with self.repo.transaction():
            rows = (
                await self.repo.session.execute(
                    text(
                        "SELECT root_node_id FROM file_trash_ops WHERE org_team_id = :org "
                        "AND purge_after <= :now ORDER BY purge_after LIMIT :limit"
                    ),
                    {"org": self.repo.scope.org_team_id, "now": now, "limit": budget},
                )
            ).fetchall()
        await self._reach("after_pick")
        purged = 0
        for row in rows:
            # One transaction per root, because `Trash.purge` is a repo caller
            # and every repo call needs an open one: a purge that failed
            # halfway would otherwise take the whole batch's work with it.
            #
            # A root already gone is not a failure: purging an ancestor takes
            # its descendants' trash ops with it, so a batch read before that
            # purge names roots the pass itself has just removed.
            try:
                async with self.repo.transaction():
                    await purge(NodeId(uuid.UUID(str(row[0]))))
            except NotFound:
                continue
            await self._reach("after_purge")
            purged += 1
        return SweepOutcome(
            name=self.name,
            swept=purged,
            scanned=len(rows),
            cursor="more" if len(rows) >= budget else None,
        )


class OperationWatchdog(Sweeper):
    """Fail operations whose worker stopped beating."""

    name = "operation_watchdog"

    async def run(
        self, now: datetime, *, cursor: str | None = None, budget: int = DEFAULT_BUDGET
    ) -> SweepOutcome:
        watchdog = self._deps.watchdog
        if watchdog is None:
            return self._skip("no operations service")
        failed = await watchdog(now)
        await self._reach("after_watchdog")
        return SweepOutcome(name=self.name, swept=len(failed), scanned=len(failed))


#: The upload-session states a lapsed lease's abort ends. A session in any
#: other state has already stopped holding quota.
SESSION_STATES_A_REAP_ABORTS: Final = ("open", "uploading", "committing")
#: The stage-job states a lapsed lease's abort ends.
STAGE_JOB_STATES_A_REAP_ABORTS: Final = ("queued", "running")

#: Reaping a lapsed lease in one statement.
#:
#: The lapse, the delay, the abort of the work the lease was driving, the
#: release of that work's quota and the retraction of everything the holder
#: said was in flight are one transaction because they are one fact: a lease
#: that lapsed while its box kept uploading must not leave a session reserving
#: quota against a holder nobody can fence any more, and must not leave the
#: drive telling readers that a file is still being written by a machine that
#: is gone. ``grantable_after`` is stamped in the same statement so the folder
#: cannot be granted to a new holder inside the window the old holder's
#: in-flight writes may still land in.
#:
#: A session belongs to the lease when its target folder is the leased node or
#: sits under it; ``path_ids`` labels are per-drive inos, so the drive has to
#: match as well as the path.
#:
#: The nodes the retraction leaves behind are left exactly as they are, and
#: that is the custody rule rather than an omission. A lapse proves only that
#: the holder stopped beating, not that its copy is gone: a file whose bytes
#: never landed exists on that machine and nowhere else, and the drive cannot
#: tell a dead machine from one cut off by an outage. So the row stays listed,
#: reads ``unsynced`` (its holder's lease is no longer live) and is the same
#: machine's to fill when it takes the folder back. Only the grace sweep below
#: may move it, and only into the trash under an op that names the machine, so
#: a restore brings it back. A file with a head keeps its last landed bytes.
_REAP_LEASES: Final = f"""
WITH reaped AS (
    UPDATE file_leases SET reaped_at = :now,
        grantable_after = :now + make_interval(secs => :delay)
    WHERE node_id IN (
        SELECT node_id FROM file_leases WHERE org_team_id = :org
        AND reaped_at IS NULL AND expires_at <= :now LIMIT :limit
    ) RETURNING node_id
), aborted_jobs AS (
    UPDATE file_stage_jobs SET state = 'cancelled'
    WHERE org_team_id = :org AND state = ANY(:live_jobs)
      AND lease_node_id IN (SELECT node_id FROM reaped)
    RETURNING id
), aborted_sessions AS (
    UPDATE file_upload_sessions s SET state = 'aborted',
        quota_hold_bytes = 0, quota_hold_nodes = 0
    WHERE s.org_team_id = :org AND s.state = ANY(:open_sessions)
      AND EXISTS (
        SELECT 1 FROM file_nodes parent, file_nodes leased
        WHERE parent.id = s.parent_id
          AND leased.id IN (SELECT node_id FROM reaped)
          AND parent.drive_id = leased.drive_id
          AND {subtree_sql("parent.path_ids", "leased.path_ids")}
      )
    RETURNING s.id
), retracted AS (
    DELETE FROM file_lease_live_entries
    WHERE org_team_id = :org AND lease_node_id IN (SELECT node_id FROM reaped)
    RETURNING node_id
)
SELECT node_id AS lease_node_id FROM reaped
"""  # noqa: S608


#: The leases whose grace has run out and whose rows have not been swept yet.
#: A lease is past its grace when it is not live and the moment it stopped
#: being live -- released, reaped, or merely lapsed -- is older than the grace.
_UNSYNCED_CANDIDATES: Final = """
SELECT l.node_id FROM file_leases l
WHERE l.org_team_id = :org AND l.unsynced_swept_at IS NULL
  AND NOT (l.released_at IS NULL AND l.reaped_at IS NULL AND l.expires_at > :now)
  AND coalesce(l.released_at, l.reaped_at, l.expires_at) <= :cutoff
ORDER BY coalesce(l.released_at, l.reaped_at, l.expires_at), l.node_id
LIMIT :limit
"""

#: One lease's grace sweep, in one statement.
#:
#: The lease is re-checked inside the statement, so a grant that committed
#: since the candidates were read -- the machine came back -- finds its rows
#: untouched. A file under a lease that is live NOW (a new holder of the folder
#: or of a folder around it) is that holder's to report and is left alone.
#: Every other file still carrying a facet under the lease loses it; the ones
#: with no head -- names whose bytes never left the machine, with or without
#: the facet that said so -- are trashed, each under a trash op of its own
#: that says why and names the machine, so the trash lists them and a restore
#: brings them back. This is the only place a holder's unlanded file leaves
#: the listing on the drive's own initiative (see ``_REAP_LEASES``).
_SWEEP_UNSYNCED: Final = f"""
WITH lease AS (
    SELECT l.node_id, l.machine_id, l.live_seq, n.drive_id, n.path_ids
    FROM file_leases l JOIN file_nodes n ON n.id = l.node_id AND n.org_team_id = l.org_team_id
    WHERE l.org_team_id = :org AND l.node_id = :lease AND l.unsynced_swept_at IS NULL
      AND NOT (l.released_at IS NULL AND l.reaped_at IS NULL AND l.expires_at > :now)
      AND coalesce(l.released_at, l.reaped_at, l.expires_at) <= :cutoff
    FOR NO KEY UPDATE OF l
), facets AS (
    SELECT c.id, c.parent_id, c.drive_id, c.head_version_id
    FROM file_nodes c, lease
    WHERE c.org_team_id = :org AND c.kind = 'file' AND c.trashed_at IS NULL
      AND (c.holder_size IS NOT NULL OR c.head_version_id IS NULL)
      AND {subtree_sql("c.path_ids", "lease.path_ids")}
      AND NOT EXISTS (
        SELECT 1 FROM file_leases o JOIN file_nodes a ON a.id = o.node_id
        WHERE o.org_team_id = :org AND o.node_id <> lease.node_id
          AND o.released_at IS NULL AND o.reaped_at IS NULL AND o.expires_at > :now
          AND {subtree_sql("c.path_ids", "a.path_ids")}
      )
), cleared AS (
    UPDATE file_nodes n SET holder_size = NULL, holder_mtime_ns = NULL,
        holder_hash = NULL, holder_seq = NULL, updated_at = :now
    FROM facets f
    WHERE n.id = f.id AND n.org_team_id = :org AND f.head_version_id IS NOT NULL
    RETURNING n.id, n.parent_id
), unlanded AS (
    SELECT f.id, f.drive_id, lease.machine_id AS machine
    FROM facets f, lease WHERE f.head_version_id IS NULL
), {trash_left_behind_sql("unlanded")}, swept AS (
    UPDATE file_leases SET unsynced_swept_at = :now
    WHERE org_team_id = :org AND node_id IN (SELECT node_id FROM lease)
    RETURNING node_id
)
SELECT 'trashed' AS what, t.id, t.parent_id, t.etag, t.op_id, NULL::text AS machine,
       NULL::uuid AS drive_id, NULL::bigint AS live_seq
FROM trashed t
UNION ALL
SELECT 'cleared', c.id, c.parent_id, NULL, NULL, NULL, NULL, NULL FROM cleared c
UNION ALL
SELECT 'lease', lease.node_id, NULL, NULL, NULL, lease.machine_id, lease.drive_id,
       lease.live_seq
FROM lease WHERE EXISTS (SELECT 1 FROM swept)
"""  # noqa: S608


class LeaseReaper(Sweeper):
    """Reap folder leases nobody released and nobody kept alive.

    The reap is one compare-and-swap: a heartbeat that lands first moves
    `expires_at` forward and the `WHERE` no longer matches, so a live holder is
    never reaped by a sweeper that read the row a moment earlier. Everything
    the reap has to undo rides that same statement, so there is no window in
    which the lease is dead but the plane it wrote still says a file is on its
    way from a machine nobody can reach.
    """

    name = "lease_reaper"

    async def run(
        self, now: datetime, *, cursor: str | None = None, budget: int = DEFAULT_BUDGET
    ) -> SweepOutcome:
        reaped = await self._reap(now, budget)
        swept, more = await self._sweep_unsynced(now, budget)
        return SweepOutcome(
            name=self.name,
            swept=reaped + swept,
            scanned=reaped + swept,
            cursor="more" if more else None,
        )

    async def _reap(self, now: datetime, budget: int) -> int:
        reap = self._deps.reap_leases
        if reap is not None:
            reaped = await reap(now)
            await self._reach("after_reap")
            return reaped
        async with self.repo.transaction():
            await self._reach("before_reap")
            rows = (
                await self.repo.session.execute(
                    text(_REAP_LEASES),
                    {
                        "org": self.repo.scope.org_team_id,
                        "now": now,
                        "limit": budget,
                        "delay": self._deps.lease_grant_delay.total_seconds(),
                        "open_sessions": list(SESSION_STATES_A_REAP_ABORTS),
                        "live_jobs": list(STAGE_JOB_STATES_A_REAP_ABORTS),
                        **SUBTREE_DEPTH_BIND,
                    },
                )
            ).fetchall()
            await self._reach("after_reap")
        return len(rows)

    async def _sweep_unsynced(self, now: datetime, budget: int) -> tuple[int, bool]:
        """Trash what leases gone longer than the grace left behind.

        Answers how many leases were swept, and whether the budget cut the
        pass short. One transaction per lease, so a lease whose sweep fails
        leaves the others done and itself for the next pass.
        """
        cutoff = now - self._deps.unsynced_grace
        async with self.repo.transaction():
            candidates = [
                row.node_id
                for row in await self.repo.session.execute(
                    text(_UNSYNCED_CANDIDATES),
                    {
                        "org": self.repo.scope.org_team_id,
                        "now": now,
                        "cutoff": cutoff,
                        "limit": budget,
                    },
                )
            ]
        swept = 0
        for lease_node_id in candidates:
            async with self.repo.transaction():
                if await self._sweep_one(lease_node_id, now=now, cutoff=cutoff):
                    swept += 1
            await self._reach("after_unsynced")
        return swept, len(candidates) >= budget

    async def _sweep_one(
        self, lease_node_id: uuid.UUID, *, now: datetime, cutoff: datetime
    ) -> bool:
        rows = (
            await self.repo.session.execute(
                text(_SWEEP_UNSYNCED),
                {
                    "org": self.repo.scope.org_team_id,
                    "lease": lease_node_id,
                    "now": now,
                    "cutoff": cutoff,
                    "actor": history.subject_ref(self._deps.ctx),
                    "window": TRASH_WINDOW.total_seconds(),
                    "reason": REASON_LEFT_ON_MACHINE,
                    **SUBTREE_DEPTH_BIND,
                },
            )
        ).all()
        lease = next((row for row in rows if row.what == "lease"), None)
        if lease is None:
            # Re-granted, or swept by another pass, since the candidates were read.
            return False
        trashed = [row for row in rows if row.what == "trashed"]
        touched = {row.parent_id for row in rows if row.what in ("trashed", "cleared")}
        removed: dict[uuid.UUID, int] = {}
        for row in trashed:
            removed[row.parent_id] = removed.get(row.parent_id, 0) - 1
        await stats.add_deltas(self.repo, removed, child_change_at=now)
        for row in trashed:
            # The row a person reads in the history pane: why the file they saw
            # listed is gone, and which machine its bytes stayed on.
            await history.record(
                self.repo,
                self._deps.ctx,
                node_id=NodeId(row.id),
                kind="trash",
                before={"parent_id": str(row.parent_id)},
                after={
                    "trash_op_id": str(row.op_id),
                    "metadata": {"reason": REASON_LEFT_ON_MACHINE, "machine": lease.machine},
                },
            )
        drive_id = DriveId(lease.drive_id)
        named = len(touched) <= UNSYNCED_MAX_NAMED_FOLDERS
        if touched and named:
            for folder in sorted(touched):
                await history.emit_node_changed(
                    self.repo,
                    self._deps.ctx,
                    node_id=NodeId(folder),
                    drive_id=drive_id,
                    version=int(lease.live_seq),
                    parent_id=NodeId(folder),
                    flush=False,
                )
            await self.repo.session.flush()
        elif touched:
            await history.emit_lease_changed(
                self.repo,
                self._deps.ctx,
                lease_node_id=NodeId(lease.id),
                drive_id=drive_id,
                live_seq=int(lease.live_seq),
                landing_count=0,
                subtree=True,
            )
        return True


#: Collapsing one generation of live writes down to what anybody asks for.
#:
#: A version stamped with the lease generation it landed under was written by
#: an agent working in a mounted folder, and an agent saves a file it is
#: building dozens of times a minute. Ordinary version retention keeps a month
#: of everything, so without this one turn leaves hundreds of rows that exist
#: only because the machine autosaved.
#:
#: Two things are never candidates and so can never be the row this deletes:
#: the node's head, and anything pinned — kept forever, held, named by a
#: content grant somebody could still redeem, or named by a conflict row.
#: Ranking runs over what is left, newest first within one node and one
#: generation, so the survivor is the
#: last state the file reached before the generation ended. The head is kept
#: on top of that rather than counted against the budget, which is what makes
#: "collapsed" mean the file still opens.
_COLLAPSE_LIVE_VERSIONS: Final = """
WITH candidates AS (
    SELECT v.id,
           row_number() OVER (
               PARTITION BY v.node_id, v.metadata ->> 'live_lease_epoch'
               ORDER BY v.seq DESC
           ) AS rank
    FROM file_versions v
    JOIN file_nodes n ON n.id = v.node_id AND n.org_team_id = v.org_team_id
    WHERE v.org_team_id = :org
      AND v.metadata ? 'live_lease_epoch'
      -- A write back a holder landed under its fence carries the stamp too,
      -- but a live session may still merge from it: those are the write-back
      -- collapse's to decide, against what the session names.
      AND v.source <> 'document_snapshot'
      AND v.created_at <= :cutoff
      AND v.keep_forever = false
      AND v.held = false
      AND (n.head_version_id IS NULL OR n.head_version_id <> v.id)
      AND NOT EXISTS (
        SELECT 1 FROM file_content_grants g WHERE g.version_id = v.id
      )
      -- A two-way write's record: the conflict row names these versions, a
      -- keep restores them, and its keys would refuse the delete.
      AND NOT EXISTS (
        SELECT 1 FROM file_conflicts c
        WHERE c.base_version_id = v.id OR c.theirs_version_id = v.id
           OR c.mine_version_id = v.id
      )
), doomed AS (
    SELECT id FROM candidates WHERE rank > :keep ORDER BY id LIMIT :limit
)
DELETE FROM file_versions v
WHERE v.id IN (SELECT id FROM doomed) AND v.org_team_id = :org
  -- The exemptions again as a predicate, because a restore or an undo can
  -- make one of the chosen rows a node's head between the read and here.
  AND v.keep_forever = false AND v.held = false
  AND NOT EXISTS (
    SELECT 1 FROM file_nodes n WHERE n.head_version_id = v.id
  )
  AND NOT EXISTS (
    SELECT 1 FROM file_conflicts c
    WHERE c.base_version_id = v.id OR c.theirs_version_id = v.id OR c.mine_version_id = v.id
  )
RETURNING v.id
"""


class LiveVersionCollapse(Sweeper):
    """Bound the version rows one generation of live writes leaves behind.

    Cold, not immediate: inside the hot window every save is still a thing a
    person can scrub back to, which is the point of watching an agent work.
    Past it the intermediate saves are noise, and one landing per generation —
    plus the head, always — is what anyone ever asks for. Inline bytes go with
    the row; store objects are left to the reachability sweep, exactly as
    ordinary version retention leaves them.
    """

    name = "live_version_collapse"

    async def run(
        self, now: datetime, *, cursor: str | None = None, budget: int = DEFAULT_BUDGET
    ) -> SweepOutcome:
        async with self.repo.transaction():
            rows = (
                await self.repo.session.execute(
                    text(_COLLAPSE_LIVE_VERSIONS),
                    {
                        "org": self.repo.scope.org_team_id,
                        "cutoff": now - LIVE_VERSION_HOT,
                        "keep": LIVE_VERSIONS_KEPT,
                        "limit": budget,
                    },
                )
            ).fetchall()
            await self._reach("after_collapse")
        return SweepOutcome(name=self.name, swept=len(rows), scanned=len(rows))


#: Which write backs a co-edited file's live session still names, read from
#: the session rows (a platform table, read as the login role): per file, the
#: session's current epoch and the version ids and etags it matched (latest
#: and history). Both are JSON arrays of strings, so the collapse asks with
#: ``?``. A record written before version ids were kept is matched by its
#: etag, and only within the session's current epoch: an etag names one
#: version of the file, and one from an epoch the session left is nothing it
#: merges from.
_LIVE_SESSION_NAMES: Final = """
SELECT d.doc_id AS node,
       d.epoch AS epoch,
       to_jsonb(array_remove(
           ARRAY[d.source_version_id::text]
           || ARRAY(SELECT r ->> 'version_id' FROM jsonb_array_elements(d.source_history) r),
           NULL
       )) AS versions,
       to_jsonb(array_remove(
           ARRAY[d.source_etag::text]
           || ARRAY(
               SELECT r ->> 'etag' FROM jsonb_array_elements(d.source_history) r
               WHERE jsonb_typeof(r -> 'etag') = 'number'
           ),
           NULL
       )) AS etags
FROM crdt_docs d
WHERE d.org_id = :org AND d.doc_type = 'file' AND d.doc_id = ANY(:docs)
"""

#: The files with a write back old enough to be a collapse candidate.
_WRITE_BACK_NODES: Final = """
SELECT DISTINCT v.node_id
FROM file_versions v
WHERE v.org_team_id = :org
  AND v.source = 'document_snapshot'
  AND v.created_at <= :cutoff
  AND v.keep_forever = false
  AND v.held = false
"""

#: Collapsing runs of a co-edited file's write backs.
#:
#: A live session writes the document back to its file a couple of seconds
#: after each burst of typing, as a ``document_snapshot`` version stamped with
#: the epoch and document position it was taken from (``origin``). An hour of
#: typing is hundreds of versions, each counted against the drive, until the
#: drive's ceiling refuses the next write back and saving stops.
#:
#: A run is the consecutive write backs (by ``seq``) of one file in one epoch:
#: any other version between two write backs, or a new epoch, starts a new
#: run. Of each run the first and last are kept, and the newest in each
#: :data:`WRITE_BACK_KEEP_EVERY` slot. Never a candidate: anything inside the
#: hot window, the head, anything pinned (kept forever, held, granted, named
#: by a conflict), a version the live session names (its latest source and
#: history, by id or by etag in its current epoch), any of the newest
#: :data:`WRITE_BACK_POSITIONS_KEPT` write backs of the session's current
#: epoch (what it looks back through when it merges), and the write back an
#: outside head names as the version it was made on (the merge it is waiting
#: for starts there).
_COLLAPSE_WRITE_BACKS: Final = """
WITH live AS (
    SELECT CAST(l.node AS uuid) AS node_id, l.epoch, l.versions, l.etags
    FROM jsonb_to_recordset(CAST(:live AS jsonb))
         AS l(node text, epoch int, versions jsonb, etags jsonb)
), ordered AS (
    SELECT v.id, v.node_id, v.seq, v.created_at, v.source,
           v.keep_forever, v.held,
           CASE WHEN v.source = 'document_snapshot'
                THEN coalesce(v.metadata #>> '{origin,epoch}', '') END AS run_key,
           CASE WHEN jsonb_typeof(v.metadata -> 'based_on_etag') = 'number'
                THEN (v.metadata ->> 'based_on_etag')::bigint END AS based_on
    FROM file_versions v
    WHERE v.org_team_id = :org AND v.node_id = ANY(:nodes)
), marked AS (
    SELECT o.*,
           CASE WHEN o.run_key IS NOT DISTINCT FROM lag(o.run_key) OVER (
               PARTITION BY o.node_id ORDER BY o.seq
           ) THEN 0 ELSE 1 END AS starts
    FROM ordered o
), runs AS (
    SELECT m.*,
           sum(m.starts) OVER (PARTITION BY m.node_id ORDER BY m.seq) AS run_no
    FROM marked m
), ranked AS (
    SELECT r.*,
           row_number() OVER (PARTITION BY r.node_id, r.run_no ORDER BY r.seq) AS from_first,
           row_number() OVER (
               PARTITION BY r.node_id, r.run_no ORDER BY r.seq DESC
           ) AS from_last,
           row_number() OVER (
               PARTITION BY r.node_id, r.run_no,
                            floor(
                                CAST(extract(epoch FROM r.created_at) AS double precision)
                                / CAST(:every AS double precision)
                            )
               ORDER BY r.seq DESC
           ) AS in_slot,
           row_number() OVER (
               PARTITION BY r.node_id, r.run_key ORDER BY r.seq DESC
           ) AS newest_in_epoch
    FROM runs r
    WHERE r.run_key IS NOT NULL
), doomed AS (
    SELECT k.id
    FROM ranked k
    JOIN file_nodes n ON n.id = k.node_id AND n.org_team_id = :org
    LEFT JOIN file_versions h ON h.id = n.head_version_id AND h.org_team_id = :org
    LEFT JOIN live l ON l.node_id = k.node_id
    WHERE k.created_at <= :cutoff
      AND k.from_first > 1 AND k.from_last > 1 AND k.in_slot > 1
      AND k.keep_forever = false
      AND k.held = false
      AND (n.head_version_id IS NULL OR n.head_version_id <> k.id)
      AND NOT EXISTS (
        SELECT 1 FROM file_content_grants g WHERE g.version_id = k.id
      )
      AND NOT EXISTS (
        SELECT 1 FROM file_conflicts c
        WHERE c.base_version_id = k.id OR c.theirs_version_id = k.id
           OR c.mine_version_id = k.id
      )
      -- What the live session names.
      AND NOT coalesce(l.node_id IS NOT NULL AND (
            l.versions ? k.id::text
         OR (k.run_key = l.epoch::text AND (
                k.newest_in_epoch <= :positions
             OR (k.based_on IS NOT NULL AND l.etags ? (k.based_on + 1)::text)
            ))
      ), false)
      -- An outside change at the head made on this write back: the merge
      -- that settles it starts from here. A write back is published at the
      -- etag after the one it names.
      AND NOT coalesce(
        h.id IS NOT NULL
        AND h.source <> 'document_snapshot'
        AND k.based_on IS NOT NULL
        AND jsonb_typeof(h.metadata -> 'based_on_etag') = 'number'
        AND (h.metadata ->> 'based_on_etag')::bigint = k.based_on + 1,
        false
      )
    ORDER BY k.id
    LIMIT :limit
)
DELETE FROM file_versions v
WHERE v.id IN (SELECT id FROM doomed) AND v.org_team_id = :org
  -- The exemptions again as a predicate, because a restore or an undo can
  -- make one of the chosen rows a node's head between the read and here.
  AND v.keep_forever = false AND v.held = false
  AND NOT EXISTS (
    SELECT 1 FROM file_nodes n WHERE n.head_version_id = v.id
  )
  AND NOT EXISTS (
    SELECT 1 FROM file_conflicts c
    WHERE c.base_version_id = v.id OR c.theirs_version_id = v.id OR c.mine_version_id = v.id
  )
RETURNING v.id
"""


class WriteBackCollapse(Sweeper):
    """Bound the versions a co-edited file's write backs leave behind.

    Cold, not immediate, and never under a live session's feet: everything
    the session could still merge from stays (see
    :data:`_COLLAPSE_WRITE_BACKS`). What survives a run reads as its history at
    a coarser grain: where it began, where it ended, and one point per slot in
    between. Inline bytes go with the row; store objects are left to the
    reachability sweep, as ordinary version retention leaves them.
    """

    name = "write_back_collapse"

    async def run(
        self, now: datetime, *, cursor: str | None = None, budget: int = DEFAULT_BUDGET
    ) -> SweepOutcome:
        org = self.repo.scope.org_team_id
        cutoff = now - WRITE_BACK_HOT
        async with self.repo.transaction():
            nodes = list(
                (
                    await self.repo.session.execute(
                        text(_WRITE_BACK_NODES), {"org": org, "cutoff": cutoff}
                    )
                ).scalars()
            )
            if not nodes:
                return SweepOutcome(name=self.name)
            live = await self._live_sessions(org, nodes)
            rows = (
                await self.repo.session.execute(
                    text(_COLLAPSE_WRITE_BACKS),
                    {
                        "org": org,
                        "nodes": nodes,
                        "cutoff": cutoff,
                        "every": int(WRITE_BACK_KEEP_EVERY.total_seconds()),
                        "positions": WRITE_BACK_POSITIONS_KEPT,
                        "live": json.dumps(live),
                        "limit": budget,
                    },
                )
            ).fetchall()
            await self._reach("after_collapse")
        swept = len(rows)
        return SweepOutcome(
            name=self.name, swept=swept, scanned=swept, cursor="more" if swept >= budget else None
        )

    async def _live_sessions(
        self, org: uuid.UUID, nodes: Sequence[uuid.UUID]
    ) -> list[dict[str, Any]]:
        """What the live sessions on ``nodes`` name, as the collapse reads it.

        The session rows are a platform table the Files role holds no grant
        on, so this one read steps out of it (to the tenant role on a bound
        session, the login on the janitor's unbound one; the statement carries
        the org itself) and back in after."""
        session = self.repo.session
        async with stepped_out(session):
            rows = (
                await session.execute(
                    text(_LIVE_SESSION_NAMES),
                    {"org": org, "docs": [str(node) for node in nodes]},
                )
            ).fetchall()
        return [
            {
                "node": row.node,
                "epoch": int(row.epoch),
                "versions": row.versions,
                "etags": row.etags,
            }
            for row in rows
        ]


class _StaleFlag(Sweeper):
    """Shared body for the two node flags a dead worker can leave behind."""

    state: ClassVar[str] = ""
    #: Whether clearing the flag must also drop the node's interned ACL.
    drops_acl: ClassVar[bool] = False

    async def run(
        self, now: datetime, *, cursor: str | None = None, budget: int = DEFAULT_BUDGET
    ) -> SweepOutcome:
        async with self.repo.transaction():
            await self._reach("before_clear")
            rows = (
                await self.repo.session.execute(
                    text(
                        "UPDATE file_nodes SET state = 'live', "
                        # Bound rather than interpolated so the statement text
                        # is the same constant for both subclasses.
                        "acl_id = CASE WHEN CAST(:drop_acl AS boolean) "
                        "THEN NULL ELSE acl_id END WHERE id IN ("
                        "SELECT n.id FROM file_nodes n WHERE n.org_team_id = :org "
                        "AND n.state = :state AND n.updated_at <= :deadline "
                        "AND NOT EXISTS (SELECT 1 FROM file_ops o WHERE "
                        "o.org_team_id = n.org_team_id AND o.drive_id = n.drive_id "
                        "AND o.state IN ('queued', 'running')) LIMIT :limit"
                        ") RETURNING id, drive_id, etag"
                    ),
                    {
                        "org": self.repo.scope.org_team_id,
                        "state": self.state,
                        "deadline": now - FLAG_STALE_AFTER,
                        "limit": budget,
                        "drop_acl": self.drops_acl,
                    },
                )
            ).fetchall()
            for node_id, drive_id, etag in rows:
                await history.emit_node_changed(
                    self.repo,
                    self._deps.ctx,
                    node_id=NodeId(uuid.UUID(str(node_id))),
                    drive_id=DriveId(uuid.UUID(str(drive_id))),
                    # The outbox rule: the etag after the change, or the last
                    # etag a row that no longer exists had. Clearing a stale
                    # flag does not bump one, so the row's own etag is it —
                    # never 0, which a consumer reads as "before everything"
                    # and would let it drop a newer snapshot it already has.
                    version=int(etag),
                )
            await self._reach("after_clear")
        return SweepOutcome(name=self.name, swept=len(rows), scanned=len(rows))


class AclRewriteDrain(Sweeper):
    """Run the ACL repairs the tree has queued, a bounded batch at a time.

    Every grant and every move marks the subtree `acl_rewriting` and writes a
    queued `file_ops` row for the repair, and nothing else in the deployment
    advances that row: a queued rewrite is work somebody has to do, not a
    deadline that passes. Until it runs, the subtree keeps the interned bodies
    of the ancestors it has left; `AclRewriteStale` below cannot help, because
    it refuses to clear a flag while any operation is queued on the drive —
    including this one — so an undrained rewrite blocks its own cleanup; and a
    reader that treats the mark as "no answer" rather than walking the chain
    (a shared chat's rung, `shared_object_role`) refuses the grantee outright
    for as long as the mark stands. So the janitor executes the repair.

    Bounded and resumable like every other sweeper: batches run until the pass
    has repaired `budget` nodes, and an operation still unfinished then keeps
    its own cursor on its row for the next pass. An operation whose row cannot
    describe a subtree is failed rather than skipped — left queued it would
    block every stale flag on that drive forever.
    """

    name = "acl_rewrite_drain"

    async def run(
        self, now: datetime, *, cursor: str | None = None, budget: int = DEFAULT_BUDGET
    ) -> SweepOutcome:
        async with self.repo.transaction():
            op_ids = [
                op.id
                for op in (
                    await self.repo.execute_scoped(
                        self.repo.select_ops()
                        .where(
                            FileOp.kind == acl.OP_ACL_REWRITE,
                            FileOp.state.in_(("queued", "running")),
                        )
                        .order_by(FileOp.created_at)
                    )
                )
                .scalars()
                .all()
            ]
        repaired = 0
        unfinished = False
        for op_id in op_ids:
            while True:
                await self._reach("before_batch")
                try:
                    async with self.repo.transaction():
                        repaired += await acl.rewrite(self.repo, OperationId(op_id))
                        running = await self._still_running(op_id)
                except InvalidRequest as exc:
                    await self._fail_op(op_id, str(exc))
                    break
                if not running:
                    break
                if repaired >= budget:
                    unfinished = True
                    break
            if unfinished:
                break
        await self._reach("after_drain")
        return SweepOutcome(
            name=self.name,
            swept=repaired,
            scanned=len(op_ids),
            cursor="more" if unfinished else None,
        )

    async def _still_running(self, op_id: uuid.UUID) -> bool:
        """Whether that batch left the operation with more of its subtree to do."""
        state = (
            await self.repo.execute_scoped(self.repo.select_ops().where(FileOp.id == op_id))
        ).scalar_one()
        return str(state.state) == "running"

    async def _fail_op(self, op_id: uuid.UUID, reason: str) -> None:
        """Take an unrunnable rewrite out of the queued set it is blocking."""
        async with self.repo.transaction():
            await self.repo.session.execute(
                self.repo.update_ops()
                .where(FileOp.id == op_id)
                .values(
                    state="failed", errors=[{"code": "acl_rewrite_unrunnable", "message": reason}]
                )
            )


class AclRewriteStale(_StaleFlag):
    """Clear an `acl_rewriting` flag whose rewrite worker is gone.

    The cache does NOT recompute itself on the next read: a reader falls
    through to the chain only while the flag stands
    (:class:`~alkera_core.files.authz.grants.FilesGrantSource`), and nothing
    that marks a subtree stale ever clears ``acl_id``. Clearing the flag on its
    own would therefore hand authority back to the interned union of the
    ancestors the node has *left* — un-revoking a removed share, re-granting
    the old audience on a subtree moved out of a shared folder. So the flag and
    the body it invalidated go in the same statement: with no cache to trust,
    the reader keeps reading the chain, which is what it was already doing, and
    the rewrite re-interns a body when it next runs.
    """

    name = "acl_rewrite_stale"
    state = "acl_rewriting"
    drops_acl = True


class MovingStale(_StaleFlag):
    """Clear a `moving` flag left by a move whose operation died.

    Every batch the dead worker committed is consistent at its boundary, so the
    check is "is there still a live operation on this drive"; when there is
    not, the subtree goes back to `live` and the move is re-driven as new work.
    """

    name = "moving_stale"
    state = "moving"


class DirStatsAggregate(Sweeper):
    """Fold the folder-stats delta table into the cache and empty it."""

    name = "dir_stats_aggregate"

    async def run(
        self, now: datetime, *, cursor: str | None = None, budget: int = DEFAULT_BUDGET
    ) -> SweepOutcome:
        aggregate = self._deps.aggregate_stats
        if aggregate is None:
            return self._skip("no stats aggregator")
        folded = await aggregate()
        await self._reach("after_aggregate")
        return SweepOutcome(name=self.name, swept=folded, scanned=folded)


class ReachabilitySweep(Sweeper):
    """Move objects no live version references any more under `deleted/`.

    The one sweeper that is not a statement: reachability spans the whole
    domain prefix, so it runs under the shard lease — exactly one mutator per
    shard — and hands the work to the mark-and-sweep that owns it. The lease is
    taken and given back around the sweep, so a worker killed mid-pass loses
    the lease at its TTL rather than wedging the shard forever, and the next
    pass resumes from the cursor the sweep saved.

    The shard row is created here when it is missing. A deployment has no
    migration that seeds one, and a sweep that quietly reported "held
    elsewhere" against a row that never existed is the failure this sweeper was
    added to end.
    """

    name = "reachability"

    async def run(
        self, now: datetime, *, cursor: str | None = None, budget: int = DEFAULT_BUDGET
    ) -> SweepOutcome:
        target = self._deps.reachability
        if target is None:
            return self._skip("no reachability janitor")
        async with self.repo.transaction():
            await self.repo.session.execute(
                text(
                    "INSERT INTO file_sweep_shards (shard, cursor) "
                    "VALUES (:shard, '{}'::jsonb) ON CONFLICT (shard) DO NOTHING"
                ),
                {"shard": target.shard},
            )
            claimed = await target.janitor.claim_shard(self.repo, target.shard)
        if not claimed:
            return self._skip("another sweeper holds the shard")
        await self._reach("after_claim")
        try:
            outcome = await target.janitor.sweep(
                target.domain_id,
                org=self.repo.scope,
                shard=target.shard,
                dry_run=False,
                budget_bytes=target.budget_bytes,
            )
        finally:
            async with self.repo.transaction():
                await target.janitor.release_shard(self.repo, target.shard)
        await self._reach("after_sweep")
        if not isinstance(outcome, SweepResult):  # pragma: no cover - dry_run is False
            return SweepOutcome(name=self.name, scanned=len(outcome.candidates))
        return SweepOutcome(
            name=self.name,
            swept=len(outcome.moved),
            scanned=len(outcome.plan.candidates),
            # The breaker abandoned the pass rather than finished it, and the
            # next one has the same work to do: report it as unfinished so the
            # janitor's caller can see the shard is not caught up.
            cursor="more" if outcome.aborted else None,
        )


#: The order the janitor runs them in: release bytes before the rows that name
#: them, fail dead work before sweeping what it left, and aggregate last so the
#: stats cache reflects everything the pass did.
JANITOR_ORDER: tuple[type[Sweeper], ...] = (
    # First: every pass below, and the collector the janitor cannot see, decide
    # what they may touch from the statement of whose bytes these are. A domain
    # whose marker the creating request could not write says nothing until this
    # settles it.
    OwnerMarkers,
    IncomingOrphans,
    DeletedExpiry,
    StoreMultipartAborts,
    ExpiredSessions,
    IdempotencyKeys,
    ContentGrantNonces,
    PageGrantNonces,
    OperationsRetention,
    HistoryCompaction,
    ConflictsAndQuarantine,
    UnreferencedAcls,
    TrashPurge,
    OperationWatchdog,
    LeaseReaper,
    # After the reaper: reaping ends a generation of live writes, so the
    # collapse in the same pass sees that generation whole rather than waiting
    # five minutes to notice it closed.
    LiveVersionCollapse,
    WriteBackCollapse,
    AclRewriteDrain,
    AclRewriteStale,
    MovingStale,
    DirStatsAggregate,
    ReachabilitySweep,
)


@dataclass(frozen=True)
class JanitorReport:
    """One pass: every sweeper's outcome, in the order they ran."""

    at: datetime
    outcomes: tuple[SweepOutcome, ...]

    @property
    def order(self) -> tuple[str, ...]:
        return tuple(outcome.name for outcome in self.outcomes)

    @property
    def swept(self) -> int:
        return sum(outcome.swept for outcome in self.outcomes)

    def by_name(self, name: str) -> SweepOutcome:
        for outcome in self.outcomes:
            if outcome.name == name:
                return outcome
        raise KeyError(name)

    @property
    def cursors(self) -> dict[str, str | None]:
        """What the next pass must be handed to resume where this one stopped."""
        return {outcome.name: outcome.cursor for outcome in self.outcomes}


async def run_janitor(
    deps: SweepDeps,
    now: datetime,
    *,
    budget: int = DEFAULT_BUDGET,
    cursors: dict[str, str | None] | None = None,
) -> JanitorReport:
    """Run every sweeper once, in order, each with its own cursor and budget.

    One sweeper's failure is not the pass's: the order matters for efficiency,
    not for correctness, and every sweeper is independent and idempotent, so
    the janitor is safe to re-run at any point.
    """
    if budget < 1:
        raise ValueError(f"budget must be >= 1, got {budget}")
    carried = dict(cursors or {})
    outcomes: list[SweepOutcome] = []
    for kind in JANITOR_ORDER:
        sweeper = kind(deps)
        outcome = await sweeper.run(now, cursor=carried.get(kind.name), budget=budget)
        outcomes.append(outcome)
        await deps.checkpoints.reach(f"janitor.after.{kind.name}")
    return JanitorReport(at=now, outcomes=tuple(outcomes))


def _session_of(key: str) -> uuid.UUID | None:
    """The session id in `incoming/<session>/…`, or `None` for a foreign key.

    The key is domain-RELATIVE. A bucket-rooted admin handle answers
    `domains/<uuid>/incoming/…`, which is a foreign key here and reads as
    session-less; :class:`DomainBoundAdmin` is what makes such a handle speak
    this namespace, and a sweeper wired without it protects nothing.
    """
    parts = key.split("/")
    if len(parts) < 2 or parts[0] != "incoming":
        return None
    try:
        return uuid.UUID(parts[1])
    except ValueError:
        return None


def _parse_when(cursor: str | None) -> datetime | None:
    if cursor is None:
        return None
    try:
        return datetime.fromisoformat(cursor)
    except ValueError:
        return None


__all__ = [
    "ACL_SWEEP_INTERVAL",
    "DEFAULT_BUDGET",
    "FLAG_STALE_AFTER",
    "HISTORY_COMPACTED_KINDS",
    "HISTORY_HOT",
    "HISTORY_KEPT_KINDS",
    "INCOMING_GRACE",
    "JANITOR_ORDER",
    "LIVE_VERSIONS_KEPT",
    "LIVE_VERSION_HOT",
    "MULTIPART_TTL",
    "NONCE_RETENTION",
    "OPERATION_RETENTION",
    "REASON_LEFT_ON_MACHINE",
    "RESOLVED_RETENTION",
    "UNSYNCED_MAX_NAMED_FOLDERS",
    "WRITE_BACK_HOT",
    "WRITE_BACK_KEEP_EVERY",
    "WRITE_BACK_POSITIONS_KEPT",
    "AclRewriteDrain",
    "AclRewriteStale",
    "ConflictsAndQuarantine",
    "ContentGrantNonces",
    "DeletedExpiry",
    "DirStatsAggregate",
    "DomainBoundAdmin",
    "ExpiredSessions",
    "HistoryCompaction",
    "IdempotencyKeys",
    "IncomingAdmin",
    "IncomingOrphans",
    "JanitorReport",
    "LeaseReaper",
    "LiveVersionCollapse",
    "MarkerVerdict",
    "MovingStale",
    "MultipartAdmin",
    "ObjectAdmin",
    "OperationWatchdog",
    "OperationsRetention",
    "OwnerMarkers",
    "PageGrantNonces",
    "ReachabilityJanitor",
    "ReachabilitySweep",
    "ReachabilityTarget",
    "StoreMultipartAborts",
    "SweepDeps",
    "SweepOutcome",
    "Sweeper",
    "TrashPurge",
    "UnreferencedAcls",
    "WriteBackCollapse",
    "run_janitor",
]
