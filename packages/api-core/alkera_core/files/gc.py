"""The janitor: the reachability sweep, its breaker and the two-phase delete.

Nothing on a request path runs this. The janitor is the one component that
holds the *admin* store handle — a handle rooted above every domain prefix —
because a sweep must list objects no tenant row references any more, and a
``DomainStore`` deliberately cannot see outside its own domain.

The sweep is mark-and-sweep with four safety properties, in the order they
matter:

``sweep_started_at`` is stamped once
    On the shard row, before reachability is computed, and never restamped for
    the life of that sweep. It is the age horizon: an object written after it
    belongs to a writer the reachability pass could not have seen, so it is
    reachable by age. A restamp would move the horizon forward under a commit
    still in flight and collect the object that commit is about to reference.

Reachability is a union of roots, not one query
    Versions of live nodes, objects staged under an open upload session,
    objects named by a live download grant, versions pinned by a hold, and
    versions of a trashed node whose purge deadline has not passed. Each root
    is a separate term so that moving the clock past *one* window releases
    exactly that root's bytes and nothing else.

The breaker aborts before the first move
    A plan that would move more than :data:`BREAKER_FRACTION` of the domain's
    live bytes is refused whole, with an alert event, rather than trimmed. A
    sweep that wants to delete a fiftieth of a customer's data is far more
    likely to be a bug in the reachability pass than that much data actually
    being garbage.

Deletion is two phase
    A candidate is *moved* to ``deleted/<key>`` and only hard-deleted by
    :meth:`Janitor.expire_deleted` once :data:`DELETED_WINDOW` has passed, so
    the week after a mistake is recoverable by moving the object back.

Mutual exclusion between two sweepers is a lease *row*
    ``UPDATE file_sweep_shards SET holder = … WHERE shard = … AND (holder IS
    NULL OR expires_at < now())`` — one statement, compare-and-swap, decided by
    Postgres. Never ``pg_advisory_lock``: a session-scoped lock does not
    survive PgBouncer transaction pooling, so two sweepers behind a pooler
    would both believe they held the shard.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Protocol

from sqlalchemy import text

from alkera_core.db.locking import AdvisoryKey, advisory_key, advisory_xact_lock
from alkera_core.events.outbox import emit
from alkera_core.files.checkpoints import Checkpoints, NoopCheckpoints
from alkera_core.files.clock import Clock
from alkera_core.files.ids import DomainId, OrgScope
from alkera_core.files.ownership import OWNER_PREFIX, read_owner
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.keys import DOMAIN_PREFIX, deleted_key
from alkera_core.files.store.protocol import ListPage
from alkera_core.files.store.scoped import ScopedStoreFactory

#: The share of a domain's live bytes a single sweep may move before the
#: breaker refuses it whole.
BREAKER_FRACTION: Final = 0.02

#: How long an object parked under ``deleted/`` stays recoverable.
DELETED_WINDOW: Final = timedelta(days=7)

#: The alert an aborted sweep records. The outbox's type registry is a closed
#: enum owned elsewhere, so the breaker rides the file-operation type and names
#: itself in the payload; an operator alert routes on ``alert``, never on prose.
BREAKER_EVENT_TYPE: Final = "file_operation.changed"
BREAKER_ALERT: Final = "file_gc.breaker"

#: How long a sweep-shard lease is held before another sweeper may take it. Named
#: for the shard rather than "LEASE_TTL" so it never competes with the file
#: lease TTL in `leases.py`, which is a different clock on a different object.
SWEEP_LEASE_TTL: Final = timedelta(minutes=15)

#: The upload-session states whose ``incoming/`` prefix is a root — the three
#: in which a session still owns its staged objects. Spelled here rather than
#: imported from the model because the library layer does not read models.
OPEN_SESSION_STATES: Final = ("open", "uploading", "committing")

#: The prefixes a sweep collects from, in the order it walks them. ``deleted/``
#: is deliberately absent: phase two owns it, and a sweep that swept its own
#: parking lot would erase the week of recoverability the two phases buy.
SWEEP_PREFIXES: Final = ("incoming/", "objects/")

#: How many keys one listing page pulls. Candidates are streamed a page at a
#: time and never accumulated whole: a domain holds more objects than a worker
#: holds memory.
LIST_PAGE: Final = 1000


class _DeletingStore(Protocol):
    """The slice of the admin handle the shared expiry helper needs."""

    async def list_prefix(
        self, prefix: str, *, after: str | None = None, limit: int = 1000
    ) -> ListPage: ...

    async def delete(self, key: str) -> None: ...


class ObjectAgeSource(Protocol):
    """When an object was written, for the age horizon.

    The store protocol's ``head`` deliberately says nothing about time — a
    portable object store need not expose one — so the age rule takes its own
    seam rather than growing the store surface. When no source is given the
    horizon never fires and only the explicit roots protect an object, which is
    the safe direction: the age rule can only ever *keep* bytes.
    """

    async def written_at(self, key: str) -> datetime | None:
        """The instant ``key`` was written, or ``None`` if unknown."""
        ...


@dataclass(frozen=True, slots=True)
class SweepCandidate:
    """One object the sweep would move, with the bytes it would free.

    ``page`` is which listing page the key was collected on, kept for the
    report: the move loop re-checks every key on its own, under that key's
    lock, so no candidate rides on an answer taken before an earlier move.
    """

    key: str
    size: int
    page: int = 0


@dataclass(frozen=True, slots=True)
class SweepPlan:
    """What a sweep intends to do. A dry run returns this and stops."""

    domain_id: DomainId
    started_at: datetime
    live_bytes: int
    candidates: tuple[SweepCandidate, ...]
    breaker_tripped: bool

    @property
    def moved_bytes(self) -> int:
        return sum(candidate.size for candidate in self.candidates)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(candidate.key for candidate in self.candidates)


@dataclass(frozen=True, slots=True)
class SweepResult:
    """What a real sweep did. ``moved`` is empty when the breaker tripped."""

    plan: SweepPlan
    moved: tuple[str, ...] = ()
    aborted: bool = False
    #: Candidates the invalidate-before-move re-check took back: bytes that
    #: gained a reference between the scan and the move. Reported rather than
    #: silent, because a sweep that keeps finding them is a sweep racing a
    #: writer often enough that an operator should know.
    skipped: tuple[str, ...] = ()

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)

    @property
    def breaker_tripped(self) -> bool:
        return self.plan.breaker_tripped


@dataclass(slots=True)
class _Roots:
    """The reachable set, kept as the union of its separately-named terms."""

    keys: set[str] = field(default_factory=set)
    prefixes: set[str] = field(default_factory=set)

    def covers(self, key: str) -> bool:
        if key in self.keys:
            return True
        return any(key.startswith(prefix) for prefix in self.prefixes)


def object_lock_key(org_team_id: uuid.UUID, key: str) -> AdvisoryKey:
    """The lock both sides of the adopt-versus-park guard take."""
    return advisory_key("files-object", org_team_id, key)


async def claim_object(repo: FilesRepo, session_id: uuid.UUID, key: str) -> None:
    """Record that an open upload session is about to adopt ``key``.

    The writer's half of the guard the sweep tries in ``Janitor._lock_object``.
    Under the object's lock, the session row names the content key. What the
    sweep then sees depends on who owns the caller's transaction, and both
    shapes are safe:

    * On a repo that owns its transaction (the worker's promote path) the
      claim COMMITS before the writer looks at the store and the lock is
      released with it. From that commit until the session closes -- in the
      transaction that inserts the version -- the claimed key is a root, so a
      sweep that re-checks afterwards lets the key go.
    * On a JOINED repo (the ``put_content`` route) ``transaction()`` is a
      savepoint inside the request's transaction: the claim row stays
      invisible and the lock stays HELD until the request commits, across
      every store call of the publish. The sweep only tries the lock, finds it
      held and skips the key; by the time the lock is free the version row is
      committed and is itself the root.

    Either way, a sweep that took the lock first finishes its move before this
    returns, and the writer then finds the object parked and takes it back.

    The caller must be inside ``repo.transaction()``, and must call this
    before its first store call for ``key``.
    """
    await advisory_xact_lock(repo.session, object_lock_key(repo.scope.org_team_id, key))
    await repo.session.execute(
        text(
            "UPDATE file_upload_sessions SET store_key = :key WHERE id = :id AND org_team_id = :org"
        ),
        {"key": key, "id": session_id, "org": repo.scope.org_team_id},
    )


class Janitor:
    """The sweeper. One instance per worker; one sweep per shard at a time."""

    def __init__(
        self,
        repo_for_org: Callable[[OrgScope], FilesRepo],
        store_factory: ScopedStoreFactory,
        clock: Clock,
        checkpoints: Checkpoints | None = None,
        *,
        age_source: ObjectAgeSource | None = None,
        holder: str = "janitor",
    ) -> None:
        self._repo_for_org = repo_for_org
        # The janitor is a crossing role: reachability spans every domain in the
        # bucket, so it takes the factory and asks for the bucket-wide handle
        # here. Naming ``ObjectStore`` in a signature is what would let a
        # request-path caller hand one in, so the widening happens once, inside
        # the one class allowed to do it.
        self._store = store_factory.admin()
        self._clock = clock
        self._checkpoints: Checkpoints = checkpoints or NoopCheckpoints()
        self._age_source = age_source
        self._holder = holder

    # -- shard leases ----------------------------------------------------

    async def claim_shard(self, repo: FilesRepo, shard: int) -> bool:
        """Take the shard's lease, or report that somebody else holds it.

        One statement: the ``WHERE`` is the compare-and-swap, so two sweepers
        racing on two connections cannot both see a free row. ``now()`` is
        Postgres's, because two workers' wall clocks disagree and the lease is
        the one thing that must not be decided by the loser's clock.
        """
        # The parameters are bound into the statement rather than passed
        # alongside it so the call is one argument: a pooled session that opens
        # a fresh connection per statement takes the statement and nothing else,
        # and the lease must be provable through exactly that session.
        result = await repo.session.execute(
            text(
                "UPDATE file_sweep_shards SET holder = :holder, "
                "expires_at = now() + :ttl "
                "WHERE shard = :shard AND (holder IS NULL OR expires_at < now()) "
                "RETURNING shard"
            ).bindparams(holder=self._holder, shard=shard, ttl=SWEEP_LEASE_TTL)
        )
        # `.all()` rather than `.first()`: the pooled session materialises its
        # rows before the connection goes back, and only the sequence survives.
        return len(result.all()) == 1

    async def release_shard(self, repo: FilesRepo, shard: int) -> None:
        """Give the shard back, but only if this janitor still holds it."""
        await repo.session.execute(
            text(
                "UPDATE file_sweep_shards SET holder = NULL, expires_at = NULL "
                "WHERE shard = :shard AND holder = :holder"
            ).bindparams(shard=shard, holder=self._holder)
        )

    async def _stamp_started(self, repo: FilesRepo, shard: int) -> datetime:
        """Stamp ``sweep_started_at`` once and return it.

        The ``IS NULL`` predicate is what makes it once: a resumed sweep — one
        killed mid-move — re-reads the stamp its first run wrote rather than
        moving the horizon forward past the writers that started in between.
        """
        await repo.session.execute(
            text(
                "UPDATE file_sweep_shards SET sweep_started_at = now() "
                "WHERE shard = :shard AND sweep_started_at IS NULL"
            ),
            {"shard": shard},
        )
        row = (
            await repo.session.execute(
                text("SELECT sweep_started_at FROM file_sweep_shards WHERE shard = :shard"),
                {"shard": shard},
            )
        ).first()
        if row is None or row[0] is None:
            msg = f"sweep shard {shard} does not exist"
            raise LookupError(msg)
        started: datetime = row[0]
        return started

    async def _cursor(self, repo: FilesRepo, shard: int) -> str | None:
        """Where the last killed run of this sweep got to, if there was one."""
        row = (
            await repo.session.execute(
                text("SELECT cursor ->> 'after' FROM file_sweep_shards WHERE shard = :shard"),
                {"shard": shard},
            )
        ).first()
        if row is None or row[0] is None:
            return None
        value = str(row[0])
        return value or None

    async def _save_cursor(self, repo: FilesRepo, shard: int, after: str) -> None:
        await repo.session.execute(
            text(
                "UPDATE file_sweep_shards "
                "SET cursor = jsonb_build_object('after', CAST(:after AS text)) "
                "WHERE shard = :shard"
            ),
            {"shard": shard, "after": after},
        )

    async def finish_shard(self, repo: FilesRepo, shard: int) -> None:
        """Clear the horizon and the cursor so the next sweep starts fresh."""
        await repo.session.execute(
            text(
                "UPDATE file_sweep_shards "
                "SET sweep_started_at = NULL, cursor = '{}'::jsonb "
                "WHERE shard = :shard"
            ),
            {"shard": shard},
        )

    # -- reachability ----------------------------------------------------

    async def _roots(self, repo: FilesRepo) -> _Roots:
        """Every object the domain still needs, as the union of its roots.

        Each ``SELECT`` is one root kind. They are written separately — rather
        than as one query with an ``OR`` — because the test for each root
        expires exactly that term and must see exactly that object collected.
        """
        roots = _Roots()
        org = str(repo.scope.org_team_id)

        # Root 1: a version of a live node. The node's state matters: a trashed
        # node is covered by root 5 only until its purge deadline, which is
        # what makes the trash window actually release bytes.
        rows = await repo.session.execute(
            text(
                "SELECT v.store_key FROM file_versions v "
                "JOIN file_nodes n ON n.id = v.node_id "
                "WHERE v.org_team_id = :org AND v.store_key IS NOT NULL "
                "AND n.trashed_at IS NULL"
            ),
            {"org": org},
        )
        roots.keys.update(row[0] for row in rows)

        # Root 2: an open upload session's staging prefix. A whole prefix, not
        # a key: the session's parts are named by the driver and the sweep must
        # not have to know that naming.
        rows = await repo.session.execute(
            text(
                "SELECT id FROM file_upload_sessions "
                "WHERE org_team_id = :org AND state = ANY(:states)"
            ),
            {"org": org, "states": list(OPEN_SESSION_STATES)},
        )
        roots.prefixes.update(f"incoming/{row[0]}/" for row in rows)

        # Root 3: an in-flight download grant. A grant already handed out is a
        # reader mid-stream; taking its bytes would truncate a live download.
        rows = await repo.session.execute(
            text(
                "SELECT v.store_key FROM file_content_grants g "
                "JOIN file_versions v ON v.id = g.version_id "
                "WHERE g.org_team_id = :org AND v.store_key IS NOT NULL "
                "AND g.expires_at > now()"
            ),
            {"org": org},
        )
        roots.keys.update(row[0] for row in rows)

        # Root 4: a held version — a legal hold or keep-forever outlives the
        # trash window and the node's state entirely.
        rows = await repo.session.execute(
            text(
                "SELECT store_key FROM file_versions "
                "WHERE org_team_id = :org AND store_key IS NOT NULL "
                "AND (held OR keep_forever)"
            ),
            {"org": org},
        )
        roots.keys.update(row[0] for row in rows)

        # Root 5: a trashed node whose purge deadline has not passed. Restore
        # must return every byte, so the trash window and the GC grace are the
        # same window by construction.
        rows = await repo.session.execute(
            text(
                "SELECT v.store_key FROM file_versions v "
                "JOIN file_nodes n ON n.id = v.node_id "
                "JOIN file_trash_ops t ON t.id = n.trash_op_id "
                "WHERE v.org_team_id = :org AND v.store_key IS NOT NULL "
                "AND t.purge_after > now()"
            ),
            {"org": org},
        )
        roots.keys.update(row[0] for row in rows)
        return roots

    async def _lock_object(self, repo: FilesRepo, key: str) -> bool:
        """Take the object's lock if nobody holds it; report whether we did.

        The uploader takes the same lock before it records the content key it
        is about to adopt (:func:`claim_object`), so "claim, then look for the
        object" and "look for a claim, then park the object" cannot interleave:
        whichever side takes the lock second sees what the first one did.

        The sweep only ever *tries*: a writer holding the lock is a writer in
        the middle of claiming this key, so the key is reachable by the time
        the sweep could act and is simply skipped this pass. Waiting would hold
        the sweep -- and its shard lease -- for as long as a same-hash upload
        takes to publish, which on a request path that carries its claim to
        the request's COMMIT is the whole upload.

        Transaction-scoped on purpose. The rule against advisory locks in this
        module is a rule against *session* locks, which a transaction pooler
        hands to the next borrower; a ``pg_*_xact_lock`` is released by the
        COMMIT of the transaction that took it and never outlives the
        connection's turn.
        """
        return await advisory_xact_lock(
            repo.session, object_lock_key(repo.scope.org_team_id, key), try_only=True
        )

    async def _reachable_now(self, repo: FilesRepo, key: str) -> bool:
        """Whether any root names ``key`` at this instant.

        The roots of :meth:`_roots`, restricted to one key and read again under
        the object's lock, immediately before its move. Asking "did a reference
        appear since the horizon" instead would miss a version whose
        transaction began before the stamp and committed after the scan: its
        ``created_at`` predates the horizon and no scan ever saw it. The
        question that matters is the one the scan asked, asked again now.

        A version the roots pass deliberately excludes (a trashed node past its
        purge deadline) is excluded here by the same terms, so the trash window
        still releases its bytes.
        """
        row = (
            await repo.session.execute(
                text(
                    "SELECT EXISTS ("
                    " SELECT 1 FROM file_versions v JOIN file_nodes n ON n.id = v.node_id"
                    " WHERE v.org_team_id = :org AND v.store_key = :key"
                    " AND n.trashed_at IS NULL"
                    ") OR EXISTS ("
                    " SELECT 1 FROM file_versions v"
                    " WHERE v.org_team_id = :org AND v.store_key = :key"
                    " AND (v.held OR v.keep_forever)"
                    ") OR EXISTS ("
                    " SELECT 1 FROM file_versions v JOIN file_nodes n ON n.id = v.node_id"
                    " JOIN file_trash_ops t ON t.id = n.trash_op_id"
                    " WHERE v.org_team_id = :org AND v.store_key = :key"
                    " AND t.purge_after > now()"
                    ") OR EXISTS ("
                    " SELECT 1 FROM file_content_grants g"
                    " JOIN file_versions v ON v.id = g.version_id"
                    " WHERE g.org_team_id = :org AND v.store_key = :key"
                    " AND g.expires_at > now()"
                    ") OR EXISTS ("
                    " SELECT 1 FROM file_upload_sessions s"
                    " WHERE s.org_team_id = :org AND s.store_key = :key"
                    " AND s.state = ANY(:states)"
                    ")"
                ),
                {
                    "org": str(repo.scope.org_team_id),
                    "key": key,
                    "states": list(OPEN_SESSION_STATES),
                },
            )
        ).first()
        return bool(row is not None and row[0])

    async def _live_bytes(self, repo: FilesRepo) -> int:
        """The bytes the breaker measures its 2 % against."""
        row = (
            await repo.session.execute(
                text(
                    "SELECT COALESCE(SUM(size_bytes), 0) FROM file_versions "
                    "WHERE org_team_id = :org AND store_key IS NOT NULL"
                ),
                {"org": str(repo.scope.org_team_id)},
            )
        ).first()
        return 0 if row is None else int(row[0])

    # -- the sweep -------------------------------------------------------

    async def sweep(
        self,
        domain_id: DomainId,
        *,
        org: OrgScope,
        shard: int = 0,
        dry_run: bool,
        budget_bytes: int | None = None,
    ) -> SweepPlan | SweepResult:
        """Plan the sweep for one dedup domain, and unless ``dry_run``, do it.

        ``org`` is passed rather than looked up: the domain-to-org map is
        platform data, and reading it here would need the very unscoped session
        the janitor refuses to own. The caller iterating orgs already holds it.

        A dry run returns the plan; a real run returns a result whose ``moved``
        set is, by construction, the plan's keys — the same code path computes
        both, so "the dry run equals the real run" is not a promise two
        implementations have to keep in step.
        """
        repo = self._repo_for_org(org)
        async with repo.transaction():
            started = await self._stamp_started(repo, shard)
            roots = await self._roots(repo)
            live_bytes = await self._live_bytes(repo)
            resume_after = await self._cursor(repo, shard)
        await self._checkpoints.reach("gc.after_reachability")

        candidates, truncated = await self._collect(
            domain_id, roots, started, resume_after, budget_bytes
        )
        would_move = sum(candidate.size for candidate in candidates)
        tripped = live_bytes > 0 and would_move > live_bytes * BREAKER_FRACTION
        plan = SweepPlan(
            domain_id=domain_id,
            started_at=started,
            live_bytes=live_bytes,
            candidates=tuple(candidates),
            breaker_tripped=tripped,
        )
        if dry_run:
            return plan
        if tripped:
            await self._alert(repo, plan)
            return SweepResult(plan=plan, moved=(), aborted=True)

        # Invalidate before move, one key at a time and under the key's lock:
        # everything between the scan and here is a window in which a commit
        # could have claimed one of these keys, and the window stays open for
        # as long as the batch takes to move. The lock, the re-check and the
        # move share one transaction, so a writer that claims the key either
        # committed its claim before the re-check read (and the key is let go),
        # holds the lock right now (and the key is let go), or waits for the
        # move and finds the object parked, which it un-parks.
        moved: list[str] = []
        skipped: list[str] = []
        for candidate in plan.candidates:
            async with repo.transaction():
                if not await self._lock_object(repo, candidate.key):
                    skipped.append(candidate.key)
                    continue
                if await self._reachable_now(repo, candidate.key):
                    skipped.append(candidate.key)
                    continue
                await self._checkpoints.reach("gc.before_move")
                await self._store.move(
                    self._absolute(domain_id, candidate.key),
                    self._absolute(domain_id, deleted_key(candidate.key)),
                )
                await self._checkpoints.reach("gc.after_move")
                moved.append(candidate.key)
                await self._save_cursor(repo, shard, candidate.key)
        if not truncated:
            # A budget-truncated collection is an unfinished sweep: clearing the
            # cursor here would send the next one back to the start of the
            # prefix, and clearing the horizon would move it forward past the
            # writers that landed while this sweep was still walking.
            async with repo.transaction():
                await self.finish_shard(repo, shard)
        return SweepResult(plan=plan, moved=tuple(moved), skipped=tuple(skipped))

    async def _collect(
        self,
        domain_id: DomainId,
        roots: _Roots,
        started: datetime,
        resume_after: str | None,
        budget_bytes: int | None,
    ) -> tuple[list[SweepCandidate], bool]:
        """Stream ``objects/`` and keep the unreachable ones.

        Returns the candidates and whether the budget cut the walk short, which
        is what tells the sweep it is not allowed to declare the shard finished.

        The listing is paged and only the surviving candidates are held, so a
        domain with a hundred million objects costs a page of keys, not a
        hundred million.
        """
        candidates: list[SweepCandidate] = []
        spent = 0
        page_index = -1
        for prefix in SWEEP_PREFIXES:
            if resume_after is not None and not resume_after.startswith(prefix):
                # A resumed sweep re-walks from where it died, so a prefix that
                # is wholly behind the cursor was finished by the first run.
                if resume_after > prefix:
                    continue
                after: str | None = None
            else:
                after = None if resume_after is None else self._absolute(domain_id, resume_after)
            while True:
                page = await self._store.list_prefix(
                    self._absolute(domain_id, prefix), after=after, limit=LIST_PAGE
                )
                page_index += 1
                for absolute_key in page.keys:
                    key = self._relative(domain_id, absolute_key)
                    if roots.covers(key):
                        continue
                    if await self._written_after(absolute_key, started):
                        continue
                    info = await self._store.head(absolute_key)
                    size = 0 if info is None else info.size
                    if budget_bytes is not None and spent + size > budget_bytes:
                        return candidates, True
                    spent += size
                    candidates.append(SweepCandidate(key=key, size=size, page=page_index))
                if page.next_after is None:
                    break
                after = page.next_after
        return candidates, False

    async def _written_after(self, absolute_key: str, started: datetime) -> bool:
        """Whether the object post-dates the horizon, so a writer owns it."""
        if self._age_source is None:
            return False
        written = await self._age_source.written_at(absolute_key)
        return written is not None and written > started

    async def _alert(self, repo: FilesRepo, plan: SweepPlan) -> None:
        """Record the refusal where an operator sees it, before returning.

        Ids and counts only: a breaker alert crosses into the platform stream
        and must not carry a name or a path.
        """
        async with repo.transaction():
            await emit(
                repo.session,
                org_id=repo.scope.org_team_id,
                type=BREAKER_EVENT_TYPE,
                entity="dedup_domain",
                entity_id=str(plan.domain_id),
                payload={
                    "alert": BREAKER_ALERT,
                    "live_bytes": plan.live_bytes,
                    "would_move_bytes": plan.moved_bytes,
                    "candidate_count": len(plan.candidates),
                },
                visibility="platform",
            )

    # -- the second phase ------------------------------------------------

    async def expire_deleted(self, domain_id: DomainId, now: datetime) -> tuple[str, ...]:
        """Hard-delete what has sat under ``deleted/`` past the window."""
        erased = await expire_deleted_objects(self._store, self._age_source, domain_id, now=now)
        return tuple(self._relative(domain_id, key) for key in erased)

    # -- keys ------------------------------------------------------------

    @staticmethod
    def _absolute(domain_id: DomainId, relative: str) -> str:
        return f"{DOMAIN_PREFIX}{domain_id}/{relative}"

    @staticmethod
    def _relative(domain_id: DomainId, absolute_key: str) -> str:
        return absolute_key[len(f"{DOMAIN_PREFIX}{domain_id}/") :]


# -- the domain collector: bytes whose whole tenant is gone ------------------

#: How long an object under a domain no drive row names is left alone. The
#: reachability sweep protects an in-flight commit with the roots it reads
#: under a live tenant; a domain with no rows at all has no roots to read, so
#: age is the only protection there is -- and it has to outlast the window
#: between "the first byte of an upload lands" and "the drive row that names
#: the domain commits". A day is far longer than that window and far shorter
#: than the time debris is worth keeping.
ORPHAN_GRACE: Final = timedelta(hours=24)

#: How many domain prefixes one pass walks before it hands back a cursor. The
#: bound is on domains rather than objects because the walk costs one listing
#: call per *known* domain and a whole prefix walk per orphan, and orphans are
#: rare.
DEFAULT_DOMAIN_BUDGET: Final = 50

#: How many objects one orphan domain gives up per pass. A pass that stops
#: early leaves the rest exactly where they are and the next pass takes them:
#: an object that has been moved is no longer under the prefix the walk reads,
#: so it resumes by construction with no cursor to keep.
DEFAULT_OBJECT_BUDGET: Final = 5000

#: The prefix a collected domain's parked objects live under. Walked for the
#: expiry, skipped by the collection walk: phase two owns it.
DELETED_PREFIX: Final = "deleted/"

#: The share of a page's own domains that may read as orphans before the pass
#: refuses to collect any of them. A tenant deleted in full is one domain; a
#: page where a tenth of the deployment reads as gone is far more likely to be
#: a database that cannot see its own rows, or the wrong database altogether.
DEFAULT_MAX_ORPHAN_FRACTION: Final = 0.1

#: What a pass concluded. ``ok`` is the only verdict under which bytes move.
VERDICT_OK: Final = "ok"
VERDICT_DISABLED: Final = "disabled"
REFUSED_KNOWN_UNREADABLE: Final = "refused.known_domains_unreadable"
REFUSED_KNOWN_EMPTY: Final = "refused.known_domains_empty"
REFUSED_NO_DEPLOYMENT: Final = "refused.no_deployment_id"
REFUSED_MASS_COLLECT: Final = "refused.mass_collect"
REFUSED_ROWS_OLDER_THAN_MARKER: Final = "refused.rows_older_than_marker"


class GcRefused(RuntimeError):  # noqa: N818 - named for what happened
    """The known-domain source cannot vouch for its answer.

    Raised by a :data:`KnownDomains` callable, never by the collector: the
    collector turns it into a report whose verdict is ``code`` and moves
    nothing, so the refusal reaches the workflow result as data rather than as
    a failure a retry policy would run again.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class KnownRows:
    """What the database says about its dedup domains, read in one breath.

    ``ids`` is every domain some drive row still names, as canonical id
    strings. ``newest_at`` is when the newest domain row was created, or
    ``None`` when there are no rows: it is what tells a database restored from
    an older snapshot apart from the one that wrote the bucket -- every domain
    the bucket gained after the snapshot carries a marker newer than any row
    the snapshot holds.
    """

    ids: frozenset[str] = frozenset()
    newest_at: datetime | None = None


KnownDomains = Callable[[], Awaitable[KnownRows]]
"""The database's dedup domains, as :class:`KnownRows`.

A seam rather than a query: the domain-to-org map is platform data, and the
library layer reads no models. The worker hands in the statements that answer
it, and a test hands in a value.
"""


async def expire_deleted_objects(
    store: _DeletingStore,
    age_source: ObjectAgeSource | None,
    domain_id: DomainId,
    *,
    now: datetime,
    window: timedelta = DELETED_WINDOW,
) -> tuple[str, ...]:
    """Hard-delete the domain's parked objects that are past ``window``.

    Returns the absolute keys it erased. With no age source nothing is erased:
    an object whose age is unknown is one the recoverability window cannot be
    proven to have passed for, and the safe reading of "unknown" is "keep".
    """
    if age_source is None:
        return ()
    cutoff = now - window
    erased: list[str] = []
    prefix = f"{DOMAIN_PREFIX}{domain_id}/{DELETED_PREFIX}"
    after: str | None = None
    while True:
        page = await store.list_prefix(prefix, after=after, limit=LIST_PAGE)
        for absolute_key in page.keys:
            written = await age_source.written_at(absolute_key)
            if written is None or written > cutoff:
                continue
            await store.delete(absolute_key)
            erased.append(absolute_key)
        if page.next_after is None:
            return tuple(erased)
        after = page.next_after


@dataclass(frozen=True, slots=True)
class DomainGcReport:
    """What one bounded collector pass did, and where the next one starts."""

    scanned: tuple[str, ...] = ()
    """Every domain prefix the pass visited, in key order."""
    orphaned: tuple[str, ...] = ()
    """The visited domains no row names any more."""
    moved: tuple[str, ...] = ()
    """Absolute keys parked under ``deleted/`` this pass."""
    erased: tuple[str, ...] = ()
    """Absolute keys hard-deleted from ``deleted/`` this pass."""
    kept_young: int = 0
    """Objects an orphan domain kept because they are inside the grace."""
    bytes_moved: int = 0
    cursor: str | None = None
    """The last domain of a full page; ``None`` when the bucket is done."""
    verdict: str = VERDICT_OK
    """``ok``, or the coded reason the pass refused to collect anything."""
    foreign: tuple[str, ...] = ()
    """Unknown domains stamped by another deployment: reported, never touched."""
    unstamped: tuple[str, ...] = ()
    """Unknown domains with no ownership marker: reported until adopted."""

    @property
    def refused(self) -> bool:
        return self.verdict != VERDICT_OK


class DomainCollector:
    """Reclaims the bytes of a dedup domain that no row names any more.

    The reachability sweep in :class:`Janitor` answers "which of THIS tenant's
    objects are still referenced", and it is driven per org from the drive
    rows -- so it can never see a domain whose rows are gone. That is not a
    hypothetical: a test run against a shared bucket writes under a domain
    whose database is dropped when the run ends, and a tenant deleted in full
    leaves the same shape. Those bytes are referenced by nothing, visited by
    nobody, and stay forever.

    So this pass walks the bucket instead of the rows: every ``domains/<id>/``
    prefix in the store, checked against the domains the deployment still
    names. A known domain is skipped whole -- its objects belong to the
    reachability sweep, which knows its roots. An unknown one is collected,
    under the same three protections the reachability sweep has:

    grace before the first move
        An object younger than :data:`ORPHAN_GRACE` is left alone, so bytes
        staged by an upload whose drive row has not committed yet are never
        taken. Age is the only root a domain with no rows has.

    unknown age keeps the bytes
        A store that will not say when an object was written protects every
        object under it. The age rule can only ever keep.

    two phase, like every other delete
        A collected object is MOVED to ``deleted/<key>`` and hard-deleted only
        once :data:`DELETED_WINDOW` has passed, so a week of mistakes is
        recoverable by moving it back.

    "No row names it" is a statement about ONE database, and an empty answer is
    exactly what a database that cannot see its own rows gives. So the pass is
    fail-closed before it is anything else, and refuses -- moving nothing, with
    a coded verdict on the report -- unless every one of these holds:

    the known set vouches for itself
        The source raises :class:`GcRefused` when it cannot prove it saw every
        tenant, and an empty set over a bucket that holds any domain is refused
        whatever the source says.

    the prefix is stamped as this deployment's
        Only a domain whose ``meta/owner.json`` names ``deployment_id`` is ever
        a candidate. A prefix another deployment stamped, and one nobody
        stamped, are reported and left exactly where they are.

    the page does not read as a mass extinction
        When more than ``max_orphan_fraction`` of the page's own domains are
        orphans the whole page is refused, unless the operator passed
        ``allow_mass_collect`` for this run.

    the database is not older than the bucket
        A database restored or cloned from the bucket's owner carries the
        owner's id, so its stamps read as its own -- and every domain created
        after the snapshot reads as an orphan. Such a domain's marker is newer
        than every domain row the snapshot holds, so an orphan stamped after
        this database's newest domain row refuses the whole run. Nothing
        overrides this: it is the one shape in which this database's own word
        cannot be trusted.

    Both bounds are per pass and resumable without state: the domain walk
    hands back the last domain it visited, and an orphan's object walk needs
    no cursor at all because a moved object is no longer under the prefix the
    next pass walks.
    """

    def __init__(
        self,
        store_factory: ScopedStoreFactory,
        clock: Clock,
        *,
        known_domains: KnownDomains,
        deployment_id: str | None,
        grace: timedelta = ORPHAN_GRACE,
        max_orphan_fraction: float = DEFAULT_MAX_ORPHAN_FRACTION,
        age_source: ObjectAgeSource | None = None,
    ) -> None:
        if not 0.0 <= max_orphan_fraction <= 1.0:
            raise ValueError(f"max_orphan_fraction must be in [0, 1], got {max_orphan_fraction}")
        # The one widening, in the one class entitled to it: a domain no row
        # names is invisible to every scoped handle by construction.
        self._store = store_factory.admin()
        self._clock = clock
        self._known_domains = known_domains
        self._deployment_id = deployment_id
        self._grace = grace
        self._max_orphan_fraction = max_orphan_fraction
        # The bucket-wide handle is its own age source on every driver that
        # can answer; a caller substitutes one only in a test.
        self._age: ObjectAgeSource | None = age_source if age_source is not None else self._store

    async def sweep(
        self,
        now: datetime | None = None,
        *,
        dry_run: bool = False,
        domain_budget: int = DEFAULT_DOMAIN_BUDGET,
        object_budget: int = DEFAULT_OBJECT_BUDGET,
        after: str | None = None,
        allow_mass_collect: bool = False,
    ) -> DomainGcReport:
        """Visit one bounded page of the bucket's domains.

        ``after`` is the domain the previous page stopped at; the walk resumes
        at the one after it. A dry run reports exactly what a real one would
        move and moves nothing. The page is classified whole before the first
        move, so a refusal never leaves a page half collected.
        """
        if domain_budget < 1:
            raise ValueError(f"domain_budget must be >= 1, got {domain_budget}")
        at = now if now is not None else self._clock.now()
        if not self._deployment_id:
            return DomainGcReport(verdict=REFUSED_NO_DEPLOYMENT)
        try:
            rows = await self._known_domains()
        except GcRefused as refusal:
            return DomainGcReport(verdict=refusal.code)
        known = rows.ids
        if not known:
            probe = await self._store.list_prefix(DOMAIN_PREFIX, after=None, limit=1)
            if probe.keys:
                return DomainGcReport(verdict=REFUSED_KNOWN_EMPTY)

        scanned: list[str] = []
        orphaned: list[str] = []
        foreign: list[str] = []
        unstamped: list[str] = []
        considered = 0
        cursor_key: str | None = None if after is None else _past_domain(after)
        while len(scanned) < domain_budget:
            page = await self._store.list_prefix(DOMAIN_PREFIX, after=cursor_key, limit=1)
            if not page.keys:
                break
            first = page.keys[0]
            domain = _domain_of(first)
            if domain is None:
                # A key directly under ``domains/`` that names no domain
                # belongs to nobody this pass can reason about. Step over it
                # rather than guess at it.
                cursor_key = first
                continue
            scanned.append(domain)
            cursor_key = _past_domain(domain)
            if domain in known:
                considered += 1
                continue
            stamp = await read_owner(self._store, domain)
            if stamp is None:
                unstamped.append(domain)
            elif stamp.deployment_id != self._deployment_id:
                foreign.append(domain)
            else:
                considered += 1
                orphaned.append(domain)
                if _stamped_after(stamp.written_at, rows.newest_at):
                    return DomainGcReport(
                        scanned=tuple(scanned),
                        orphaned=tuple(orphaned),
                        foreign=tuple(foreign),
                        unstamped=tuple(unstamped),
                        verdict=REFUSED_ROWS_OLDER_THAN_MARKER,
                    )

        cursor = scanned[-1] if len(scanned) == domain_budget else None
        tripped = bool(orphaned) and len(orphaned) > considered * self._max_orphan_fraction
        if tripped and not allow_mass_collect:
            return DomainGcReport(
                scanned=tuple(scanned),
                orphaned=tuple(orphaned),
                foreign=tuple(foreign),
                unstamped=tuple(unstamped),
                cursor=cursor,
                verdict=REFUSED_MASS_COLLECT,
            )

        moved: list[str] = []
        erased: list[str] = []
        young = 0
        bytes_moved = 0
        for domain in orphaned:
            domain_id = DomainId(uuid.UUID(domain))
            page_moved, page_bytes, page_young = await self._collect_domain(
                domain_id, at, dry_run=dry_run, budget=object_budget
            )
            moved.extend(page_moved)
            bytes_moved += page_bytes
            young += page_young
            if not dry_run:
                erased.extend(
                    await expire_deleted_objects(self._store, self._age, domain_id, now=at)
                )
        return DomainGcReport(
            scanned=tuple(scanned),
            orphaned=tuple(orphaned),
            moved=tuple(moved),
            erased=tuple(erased),
            kept_young=young,
            bytes_moved=bytes_moved,
            cursor=cursor,
            foreign=tuple(foreign),
            unstamped=tuple(unstamped),
        )

    async def _collect_domain(
        self, domain_id: DomainId, now: datetime, *, dry_run: bool, budget: int
    ) -> tuple[tuple[str, ...], int, int]:
        """Park one orphan domain's aged objects. Returns (moved, bytes, young)."""
        cutoff = now - self._grace
        prefix = f"{DOMAIN_PREFIX}{domain_id}/"
        parked = f"{prefix}{DELETED_PREFIX}"
        marker = f"{prefix}{OWNER_PREFIX}"
        moved: list[str] = []
        young = 0
        total = 0
        after: str | None = None
        while len(moved) < budget:
            page = await self._store.list_prefix(prefix, after=after, limit=LIST_PAGE)
            for absolute_key in page.keys:
                if absolute_key.startswith((parked, marker)):
                    # The parking lot is phase two's, and the ownership marker
                    # is what lets the expiry still recognise this prefix.
                    continue
                if len(moved) >= budget:
                    return tuple(moved), total, young
                written = None if self._age is None else await self._age.written_at(absolute_key)
                if written is None or written > cutoff:
                    young += 1
                    continue
                info = await self._store.head(absolute_key)
                size = 0 if info is None else info.size
                total += size
                moved.append(absolute_key)
                if dry_run:
                    continue
                relative = absolute_key[len(prefix) :]
                await self._store.move(absolute_key, f"{prefix}{deleted_key(relative)}")
            if page.next_after is None:
                break
            after = page.next_after
        return tuple(moved), total, young


def _stamped_after(written_at: datetime | None, newest_row_at: datetime | None) -> bool:
    """Whether a marker post-dates every domain row this database holds.

    A marker with no readable time, or a database with no domain rows at all,
    both read as "after": neither can prove the database saw the domain, and
    the guard exists for exactly the case where it did not.
    """
    if written_at is None or newest_row_at is None:
        return True
    return written_at > newest_row_at


def _domain_of(absolute_key: str) -> str | None:
    """The domain id ``absolute_key`` lives under, or ``None`` if it has none."""
    if not absolute_key.startswith(DOMAIN_PREFIX):
        return None
    rest = absolute_key[len(DOMAIN_PREFIX) :]
    head, slash, _ = rest.partition("/")
    if not slash:
        return None
    try:
        parsed = uuid.UUID(head)
    except ValueError:
        return None
    return None if str(parsed) != head else head


def _past_domain(domain: str) -> str:
    """A listing token just past every key under ``domains/<domain>/``.

    ``0`` sorts immediately after ``/``, and a domain id is a fixed-length
    uuid, so this skips exactly one domain's keys and never the next domain's.
    """
    return f"{DOMAIN_PREFIX}{domain}0"


__all__ = [
    "BREAKER_ALERT",
    "BREAKER_EVENT_TYPE",
    "BREAKER_FRACTION",
    "DEFAULT_DOMAIN_BUDGET",
    "DEFAULT_MAX_ORPHAN_FRACTION",
    "DEFAULT_OBJECT_BUDGET",
    "DELETED_PREFIX",
    "DELETED_WINDOW",
    "LIST_PAGE",
    "OPEN_SESSION_STATES",
    "ORPHAN_GRACE",
    "REFUSED_KNOWN_EMPTY",
    "REFUSED_KNOWN_UNREADABLE",
    "REFUSED_MASS_COLLECT",
    "REFUSED_NO_DEPLOYMENT",
    "REFUSED_ROWS_OLDER_THAN_MARKER",
    "SWEEP_LEASE_TTL",
    "SWEEP_PREFIXES",
    "VERDICT_DISABLED",
    "VERDICT_OK",
    "DomainCollector",
    "DomainGcReport",
    "GcRefused",
    "Janitor",
    "KnownDomains",
    "KnownRows",
    "ObjectAgeSource",
    "SweepCandidate",
    "SweepPlan",
    "SweepResult",
    "claim_object",
    "expire_deleted_objects",
    "object_lock_key",
]
