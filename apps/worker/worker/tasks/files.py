"""The Files plane's async cores: the upload commit, the reconciliation pass
and the resumable ACL cache rewrite.

Each core is the whole job, callable with nothing but its arguments — the
activity around it adds a heartbeat and a name, and the workflow adds the retry
policy. Everything the library reaches for (an org-scoped :class:`FilesRepo`,
the org's domain-bound store, the acting context the platform acts under, the
services' bound sweeper methods) arrives through :class:`FilesJobDeps`,
resolved by a module-level factory. The alternative — building an S3 client and
a principal at import — would make every core unreachable from a test without
credentials, and would decide who the worker acts as in the one place nobody
can substitute.

``promote`` is the commit half of an upload: the request already agreed with the
client on the parts and handed back a queued operation, and this turns the
staged bytes into a version. The library re-authorizes and re-reads the target
before a byte moves, so an upload that opened an hour ago cannot land in a
folder the caller has since lost.

``janitor`` runs every sweeper in :data:`~alkera_core.files.sweepers.JANITOR_ORDER`
once per org that owns a Files drive, in that order — the dir-stats aggregation
and the lease reaper among them, so neither needs a schedule of its own. The
order matters for efficiency, not correctness: every sweeper is independent,
idempotent and budgeted, which is why a pass is safe to re-run and why one org
that refuses never costs the rest their sweep. It is bounded in the same way
its sweepers are: a call sweeps at most one page of orgs and returns the org it
stopped at, and the workflow feeds that cursor back until a page runs short, so
the cost of an activity is the page size rather than the tenant count.

``acl_rewrite`` advances one subtree's cache repair by one batch and reports how
many nodes it fixed; the cursor lives on the operation row, so a worker killed
mid-batch loses that batch and no more.

``large_move`` drives the batched rewrite a subtree too big to move inside a
request was queued as. It resumes rather than restarts, for the same reason:
the plan and the cursor are on the operation row.

``bulk`` drives a batch of tree changes too long to hold a request open for.
Its plan carries the decision the request already took for each item, so the
worker executes the allowed ones and never re-authorizes: the plan is the
capability, exactly as a copy's is.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from alkera_core.authz.principal import ActingContext
from alkera_core.config import Settings, get_settings
from alkera_core.db.cross_tenant import CrossTenantReadRefused, cross_tenant_read
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files import acl, gc, sweepers
from alkera_core.files.clock import Clock, SystemClock
from alkera_core.files.ids import OperationId
from alkera_core.files.ops import (
    QUEUED_STALE_AFTER,
    RUNNER_NUDGED,
    Operations,
    RecoveryOutcome,
)
from alkera_core.files.ownership import deployment_id_for, marker_key, read_owner, write_marker
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.scoped import DomainStore, ScopedStoreFactory
from alkera_core.files.sweepers import JanitorReport, MarkerVerdict, SweepDeps
from alkera_core.logging import get_logger
from alkera_core.observability import metrics
from alkera_core.temporal import QUEUE_FOR, TaskQueue, WorkflowType
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = get_logger(__name__)

JANITOR_QUEUE: TaskQueue = QUEUE_FOR[WorkflowType.FILES_JANITOR]
"""The queue that runs the reconciliation pass."""

GC_QUEUE: TaskQueue = QUEUE_FOR[WorkflowType.FILES_GC]
"""The queue that runs the daily collection of orphaned bytes."""


@dataclass(frozen=True)
class FilesJobDeps:
    """One org's world, as a Files job sees it."""

    repo: FilesRepo
    ctx: ActingContext
    clock: Clock = field(default_factory=SystemClock)
    store: DomainStore | None = None
    """The org's domain-bound store, for the jobs that move bytes. ``None`` for
    a pass that only touches rows."""
    sweep: SweepDeps | None = None
    """The sweepers' injected dependencies, when the caller has the services
    whose bound methods the delegating sweepers run. Left ``None``, the janitor
    builds the row-only set and each delegating sweeper reports itself skipped
    rather than silently doing nothing."""


class FilesJobsNotWiredError(RuntimeError):
    """A Files job ran with no deps factory.

    Raised rather than defaulted: a worker acts for the platform, and which
    principal it acts as — and which bucket it writes to — is a deployment
    decision, never a fallback this module is entitled to invent.
    """


DepsFactory = Callable[[AsyncSession, uuid.UUID], Awaitable[FilesJobDeps] | FilesJobDeps]


async def _unwired(session: AsyncSession, org_team_id: uuid.UUID) -> FilesJobDeps:
    raise FilesJobsNotWiredError(
        "no Files deps factory is installed; call worker.tasks.files.set_deps_factory() "
        "with one that builds the org's FilesRepo, ActingContext and DomainStore"
    )


_deps_factory: DepsFactory = _unwired

#: The bucket-wide store factory, for the one job that is not per-org. The
#: collector walks the whole bucket -- that is the point of it -- so it cannot
#: take a handle from the per-org deps, and a deployment that does not serve
#: Files leaves this ``None`` so the job refuses instead of inventing a bucket.
_store_factory: ScopedStoreFactory | None = None


def set_store_factory(factory: ScopedStoreFactory) -> None:
    """Point the bucket-wide jobs at this deployment's store."""
    global _store_factory
    _store_factory = factory


def reset_store_factory() -> None:
    """Leave the bucket-wide jobs unwired again."""
    global _store_factory
    _store_factory = None


def set_deps_factory(factory: DepsFactory) -> None:
    """Point the cores at a way of building their world.

    The seam a deployment uses to hand the jobs a real ``DomainStore`` and the
    platform's acting context, and the seam a test uses to hand them a
    filesystem store under ``tmp_path``.
    """
    global _deps_factory
    _deps_factory = factory


def reset_deps_factory() -> None:
    """Leave the jobs unwired again — what a test undoes its wiring with."""
    global _deps_factory
    _deps_factory = _unwired


async def _deps(session: AsyncSession, org_team_id: uuid.UUID) -> FilesJobDeps:
    built: Any = _deps_factory(session, org_team_id)
    if isinstance(built, FilesJobDeps):
        return built
    awaited: FilesJobDeps = await built
    return awaited


@asynccontextmanager
async def _session() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session


async def orgs_with_drives(
    session: AsyncSession, *, after: uuid.UUID | None = None, limit: int | None = None
) -> list[uuid.UUID]:
    """One page of the orgs that own at least one Files drive, in id order.

    The janitor has nothing to do for an org that has never used Files, and the
    drive rows are how it knows — there is no separate registry to drift. The
    id order is what makes a page resumable: ``after`` is the last org the
    previous page covered, so the sequence of pages visits every org exactly
    once even though each runs in its own activity.

    ``file_drives`` is a tenant table under a FORCE'd policy and this read has
    no tenant, so it goes through the cross-tenant seam: a login the policy
    binds is refused out loud rather than answering "no orgs" and letting the
    janitor sweep nobody, every five minutes, with nothing in the log.
    """
    statement = "SELECT DISTINCT org_team_id FROM file_drives"
    params: dict[str, Any] = {}
    if after is not None:
        statement += " WHERE org_team_id > CAST(:after AS uuid)"
        params["after"] = str(after)
    statement += " ORDER BY org_team_id"
    if limit is not None:
        statement += " LIMIT :limit"
        params["limit"] = limit
    async with cross_tenant_read(session, reason="files.janitor.orgs_with_drives") as read:
        rows = (await read.execute(text(statement), params)).fetchall()
    return [uuid.UUID(str(row[0])) for row in rows]


async def promote(op_id: OperationId, org_team_id: uuid.UUID) -> str:
    """Turn the staged parts of the upload ``op_id`` was queued for into a version.

    The session is read from the operation row, where ``complete`` bound it, so
    a promote takes ``(op_id, org)`` like every other queued kind and a lost
    hand-off is recoverable from the row alone. Returns the version id, which
    is what the operation resource reports to the client polling it.
    """
    from alkera_core.files.uploads import UploadCompletion

    async with _session() as session:
        deps = await _deps(session, org_team_id)
        if deps.store is None:
            raise FilesJobsNotWiredError("files.promote needs a DomainStore; the deps carry none")
        completion = UploadCompletion(deps.repo, deps.ctx, deps.clock, deps.store)
        info = await completion.promote_operation(op_id)
        return str(info.id)


@dataclass(frozen=True)
class JanitorPass:
    """One bounded janitor pass: the orgs it swept, and where to resume.

    ``cursor`` is the last org the page covered when the page filled its
    budget — hand it to the next pass and it starts at the org after it — and
    ``None`` when the page ran short, which is how a caller knows the fleet is
    done. It names the last org of the PAGE, not the last org swept, so an org
    whose sweep raised is passed rather than handed back forever.
    """

    at: datetime
    orgs: tuple[uuid.UUID, ...]
    reports: tuple[JanitorReport, ...]
    cursor: uuid.UUID | None = None

    @property
    def swept(self) -> int:
        return sum(report.swept for report in self.reports)

    def __iter__(self) -> Iterator[JanitorReport]:
        """A pass reads as the reports it produced.

        The pass used to BE that list, and a caller that only wants the
        per-org reports should not have to learn about paging to keep them.
        """
        return iter(self.reports)

    def __len__(self) -> int:
        return len(self.reports)


async def janitor(
    now: datetime | None = None,
    *,
    budget: int = sweepers.DEFAULT_BUDGET,
    org_budget: int | None = None,
    after: uuid.UUID | None = None,
) -> JanitorPass:
    """Run every sweeper once, in order, for one bounded page of the orgs that
    use Files.

    A pass covers at most ``org_budget`` orgs (the deployment's
    ``FILES_JANITOR_ORG_BUDGET`` unless the caller pins it) and returns the org
    it stopped at, so the fleet is swept by a sequence of bounded activities
    instead of one that grows with the tenant count until a Temporal timeout
    kills it mid-pass — a killed unbounded pass would start again at the first
    org every tick and the tail would never be swept at all.
    """
    at = now or datetime.now(UTC)
    page_size = org_budget if org_budget is not None else get_settings().files_janitor_org_budget
    if page_size < 1:
        raise ValueError(f"org_budget must be >= 1, got {page_size}")
    reports: list[JanitorReport] = []
    swept: list[uuid.UUID] = []
    page: list[uuid.UUID] = []
    async with _session() as session:
        page = await orgs_with_drives(session, after=after, limit=page_size)
        for org_team_id in page:
            deps = await _deps(session, org_team_id)
            sweep = deps.sweep or SweepDeps(repo=deps.repo, ctx=deps.ctx, clock=deps.clock)
            try:
                report = await sweepers.run_janitor(sweep, at, budget=budget)
            except Exception:
                log.exception("files.janitor.org_failed", org_team_id=str(org_team_id))
                continue
            reports.append(report)
            swept.append(org_team_id)
            log.info(
                "files.janitor.org_swept",
                org_team_id=str(org_team_id),
                swept=report.swept,
                order=list(report.order),
            )
    cursor = page[-1] if len(page) == page_size else None
    log.info(
        "files.janitor.pass_done",
        orgs=len(page),
        swept_orgs=len(swept),
        cursor=str(cursor) if cursor is not None else None,
    )
    return JanitorPass(at=at, orgs=tuple(swept), reports=tuple(reports), cursor=cursor)


async def known_domains(session: AsyncSession) -> set[str]:
    """Every dedup domain some drive row still names, as canonical id strings."""
    return set((await known_rows(session)).ids)


async def known_rows(session: AsyncSession) -> gc.KnownRows:
    """The drive rows' dedup domains, and when the newest domain row was made.

    The drive rows are the whole answer: a domain exists because a drive was
    created for it, and nothing else in the schema names one. A domain missing
    from this set has no org, no versions and no sessions -- there is no row
    left anywhere that could reference a byte under it.

    ``file_drives`` is a tenant table under a FORCE'd policy, and this read has
    no tenant: it goes through the cross-tenant seam, which refuses a login the
    policy would silently filter. The answer is then checked against the
    planner's own row estimate -- "no drives" from a table Postgres believes
    holds rows is a read that did not see them -- and either failure raises
    :class:`~alkera_core.files.gc.GcRefused` rather than returning a set the
    collector would read as "every domain is an orphan".
    """
    try:
        async with cross_tenant_read(session, reason="files.gc.known_domains") as read:
            rows = (
                await read.execute(text("SELECT DISTINCT dedup_domain_id FROM file_drives"))
            ).fetchall()
            newest_row = (
                await read.execute(text("SELECT max(created_at) FROM dedup_domains"))
            ).first()
            estimate_row = (
                await read.execute(
                    text("SELECT reltuples FROM pg_class WHERE oid = 'file_drives'::regclass")
                )
            ).first()
    except CrossTenantReadRefused as exc:
        raise gc.GcRefused(gc.REFUSED_KNOWN_UNREADABLE, str(exc)) from exc
    estimate = 0.0 if estimate_row is None else float(estimate_row[0])
    if not rows and estimate > 0:
        raise gc.GcRefused(
            gc.REFUSED_KNOWN_UNREADABLE,
            f"file_drives read as empty while the planner estimates {estimate:.0f} rows",
        )
    return gc.KnownRows(
        ids=frozenset(str(uuid.UUID(str(row[0]))) for row in rows),
        newest_at=None if newest_row is None else newest_row[0],
    )


@dataclass(frozen=True)
class GcPass:
    """One bounded collector pass: what it reclaimed, and where to resume."""

    at: datetime
    scanned: tuple[str, ...] = ()
    orphaned: tuple[str, ...] = ()
    moved: tuple[str, ...] = ()
    erased: tuple[str, ...] = ()
    kept_young: int = 0
    bytes_moved: int = 0
    cursor: str | None = None
    """The domain the next pass resumes after; ``None`` when the bucket is done."""
    verdict: str = gc.VERDICT_OK
    """``ok``, ``disabled``, or the coded reason the pass refused to collect."""
    foreign: tuple[str, ...] = ()
    unstamped: tuple[str, ...] = ()


async def files_gc(
    now: datetime | None = None,
    *,
    dry_run: bool = False,
    domain_budget: int = gc.DEFAULT_DOMAIN_BUDGET,
    object_budget: int = gc.DEFAULT_OBJECT_BUDGET,
    after: str | None = None,
    grace: timedelta = gc.ORPHAN_GRACE,
    allow_mass_collect: bool = False,
    config: Settings | None = None,
) -> GcPass:
    """Collect the bytes of dedup domains no drive row names any more.

    The per-org janitor sweeps the tenants it can see; this is the pass for
    the ones it cannot. It walks the bucket's ``domains/`` prefixes, checks
    each against the drive rows, and parks an unknown domain's aged objects
    under ``deleted/`` -- two phase, so a week of them is recoverable -- then
    hard-deletes what has been parked past that window.

    Bounded like every other Files pass: one call visits at most
    ``domain_budget`` domains and hands back the one it stopped at.

    A deployment that does not serve Files, or has not turned the pass on,
    answers ``disabled`` and touches nothing -- a clean result, not a failure,
    so the schedule that exists everywhere costs one line a day. Every refusal
    the collector can reach is likewise a result with a coded verdict, logged
    at error: retrying a refusal would only refuse again.
    """
    at = now or datetime.now(UTC)
    resolved = config or get_settings()
    if not resolved.files_gc_active:
        log.info(
            "files.gc.skipped",
            files_enabled=resolved.files_enabled,
            files_gc_enabled=resolved.files_gc_enabled,
        )
        return GcPass(at=at, verdict=gc.VERDICT_DISABLED)
    factory = _store_factory
    if factory is None:
        raise FilesJobsNotWiredError(
            "files.gc needs a bucket-wide store; call worker.tasks.files.set_store_factory() "
            "with the deployment's ScopedStoreFactory"
        )
    async with _session() as session:
        deployment_id = await deployment_id_for(session, resolved)
        await session.rollback()

        async def _known() -> gc.KnownRows:
            return await known_rows(session)

        collector = gc.DomainCollector(
            factory,
            SystemClock(),
            known_domains=_known,
            deployment_id=deployment_id,
            grace=grace,
            max_orphan_fraction=resolved.files_gc_max_orphan_fraction,
        )
        report = await collector.sweep(
            at,
            dry_run=dry_run,
            domain_budget=domain_budget,
            object_budget=object_budget,
            after=after,
            allow_mass_collect=allow_mass_collect,
        )
    # Counts and the verdict, never keys: a pass may park thousands of objects,
    # and an object key is a content hash under a tenant's domain id.
    emit_line = log.error if report.refused else log.info
    emit_line(
        "files.gc.pass_done",
        verdict=report.verdict,
        domains_scanned=len(report.scanned),
        orphan_domains=len(report.orphaned),
        foreign_domains=len(report.foreign),
        unstamped_domains=len(report.unstamped),
        objects_parked=len(report.moved),
        bytes_parked=report.bytes_moved,
        objects_erased=len(report.erased),
        kept_inside_grace=report.kept_young,
        dry_run=dry_run,
        allow_mass_collect=allow_mass_collect,
        cursor=report.cursor,
    )
    return GcPass(
        at=at,
        scanned=report.scanned,
        orphaned=report.orphaned,
        moved=report.moved,
        erased=report.erased,
        kept_young=report.kept_young,
        bytes_moved=report.bytes_moved,
        cursor=report.cursor,
        verdict=report.verdict,
        foreign=report.foreign,
        unstamped=report.unstamped,
    )


async def _domain_rows_created_at(
    session: AsyncSession, domain_ids: set[str]
) -> dict[str, datetime]:
    """When each of ``domain_ids`` that has a row was created; absent ids are
    simply missing from the answer."""
    if not domain_ids:
        return {}
    try:
        async with cross_tenant_read(session, reason="files.gc.adopt") as read:
            rows = (
                await read.execute(
                    text("SELECT id, created_at FROM dedup_domains WHERE id = ANY(:ids)"),
                    {"ids": [uuid.UUID(domain) for domain in domain_ids]},
                )
            ).fetchall()
    except CrossTenantReadRefused as exc:
        raise gc.GcRefused(gc.REFUSED_KNOWN_UNREADABLE, str(exc)) from exc
    return {str(uuid.UUID(str(row[0]))): row[1] for row in rows}


async def stamp_domain_marker(
    session: AsyncSession,
    factory: ScopedStoreFactory,
    domain_id: uuid.UUID,
    *,
    created_at: datetime,
    config: Settings | None = None,
    deployment_id: str | None = None,
) -> MarkerVerdict:
    """Say ``domain_id``'s prefix is this deployment's, and report whose it is.

    What the janitor's ``owner_markers`` sweeper runs for the domains whose
    creating request could not reach the store. It is NOT an adoption: the
    marker is never overwritten, so a prefix another deployment stamped reads
    back as ``foreign`` rather than being claimed, and a prefix already carrying
    our own marker answers ``ours`` without a write.

    ``foreign`` is an answer, not a failure, and it is the one an operator has
    to see: it means this database names bytes it does not own — a bucket shared
    with, or restored from, another stack — so nothing here will ever collect
    them. It is logged once per domain with the id of the deployment that does
    own it (an id, never a byte of the prefix) and counted on
    ``files_domain_marker_foreign_total``; the sweeper records it on the row, so
    the line is not repeated every five minutes.

    Dated as the domain row was created, exactly as the adopt command dates
    what it stamps: the marker's time is what a restore guard compares against
    this database's newest domain, so a marker written late must not read as
    younger than the bytes it covers.

    ``deployment_id`` is this database's id for the configured bucket. It does
    not change while the process runs, so a caller sweeping a page of domains
    reads it once and hands it in; left out, it is read here.
    """
    resolved = config or get_settings()
    domain = str(domain_id)
    try:
        if deployment_id is None:
            deployment_id = await deployment_id_for(session, resolved)
        if deployment_id is None:
            return "unknown"
        admin = factory.admin()
        if await write_marker(admin, deployment_id, key=marker_key(domain), now=created_at):
            return "ours"
        stamp = await read_owner(admin, domain)
    except Exception:
        # One org's unreachable store is not the janitor pass's failure, and it
        # is not this domain's either: the row stays owed and the next tick
        # tries again.
        log.warning("files.ownership.stamp_retry_failed", domain_id=domain, exc_info=True)
        return "unknown"
    if stamp is None:
        # The write said the key was taken and the read cannot see it. Nothing
        # is safe to conclude from that, so nothing is recorded.
        return "unknown"
    if stamp.deployment_id == deployment_id:
        return "ours"
    log.warning(
        "files.ownership.foreign_domain",
        domain_id=domain,
        owner_deployment_id=stamp.deployment_id,
        this_deployment_id=deployment_id,
    )
    metrics.record_foreign_domain_marker()
    return "foreign"


class DomainMarkerRepair:
    """One janitor pass's marker repair for one org.

    It exists to hold the deployment id: that is one row of ``file_stores`` for
    the configured bucket, it cannot change while the process runs, and reading
    it per domain made a page of a hundred domains a hundred identical selects.
    Read once, on the first domain the pass actually asks about — an org with
    nothing owed never reads it at all.

    Its ABSENCE is held too, and that is the case worth stating: a deployment
    with no ``file_stores`` row for the configured bucket answers ``None``,
    which is exactly the value "not read yet" would be, so a memo keyed on the
    value alone would re-read it for every domain of every page — the one shape
    where the reads are pure waste, since nothing can be settled without a row.
    """

    def __init__(
        self,
        session: AsyncSession,
        factory: ScopedStoreFactory,
        *,
        config: Settings | None = None,
    ) -> None:
        self._session = session
        self._factory = factory
        self._config = config
        self._deployment_id: str | None = None
        self._read = False

    async def __call__(self, domain_id: uuid.UUID, created_at: datetime) -> MarkerVerdict:
        if not self._read:
            self._deployment_id = await deployment_id_for(
                self._session, self._config or get_settings()
            )
            self._read = True
        if self._deployment_id is None:
            # Nothing to settle: the marker names this deployment by its
            # ``file_stores`` row, and there is no row. Answered here rather
            # than passed on, because ``None`` is also how the write below
            # spells "read it yourself" — handing it over would put the read
            # back on every domain of every page.
            return "unknown"
        return await stamp_domain_marker(
            self._session,
            self._factory,
            domain_id,
            created_at=created_at,
            config=self._config,
            deployment_id=self._deployment_id,
        )


@dataclass(frozen=True)
class AdoptReport:
    """What one adopt run stamped, and what it left alone."""

    stamped: tuple[str, ...] = ()
    already_ours: tuple[str, ...] = ()
    foreign: tuple[str, ...] = ()


async def files_gc_adopt(
    domains: tuple[str, ...] = (),
    *,
    known: bool = False,
    config: Settings | None = None,
) -> AdoptReport:
    """Stamp unmarked domain prefixes as this deployment's.

    The operator's explicit statement that bytes written before the ownership
    marker existed belong here. ``known`` stamps every domain this database's
    drive rows name; ``domains`` names prefixes one by one -- the only way a
    prefix no row names becomes collectable. A prefix another deployment
    stamped is never re-stamped: it is reported and left alone.
    """
    resolved = config or get_settings()
    factory = _store_factory
    if factory is None:
        raise FilesJobsNotWiredError("files.gc adopt needs the deployment's ScopedStoreFactory")
    admin = factory.admin()
    async with _session() as session:
        deployment_id = await deployment_id_for(session, resolved)
        await session.rollback()
        if deployment_id is None:
            raise gc.GcRefused(
                gc.REFUSED_NO_DEPLOYMENT, "this database has no file_stores row for the bucket"
            )
        targets = {str(uuid.UUID(domain)) for domain in domains}
        rows = await known_rows(session)
        if known:
            targets |= set(rows.ids)
        created = await _domain_rows_created_at(session, targets)
    # The marker's time is what the restore guard compares against this
    # database's newest domain row, so an adopted domain is dated as this
    # database would have dated it: its own row's creation when the row
    # exists, else the newest row -- the operator's statement that this
    # database is at least as new as the prefix it is adopting.
    fallback = rows.newest_at or datetime.now(UTC)
    stamped: list[str] = []
    ours: list[str] = []
    foreign: list[str] = []
    for domain in sorted(targets):
        stamp = await read_owner(admin, domain)
        if stamp is not None and stamp.deployment_id == deployment_id:
            ours.append(domain)
        elif stamp is not None:
            foreign.append(domain)
        elif await write_marker(
            admin, deployment_id, key=marker_key(domain), now=created.get(domain, fallback)
        ):
            stamped.append(domain)
        else:
            # Somebody stamped it between the read and the write; whoever it
            # was, it is not this run's to claim.
            foreign.append(domain)
    log.info(
        "files.gc.adopt_done",
        stamped=len(stamped),
        already_ours=len(ours),
        foreign=len(foreign),
    )
    return AdoptReport(stamped=tuple(stamped), already_ours=tuple(ours), foreign=tuple(foreign))


async def bulk(op_id: OperationId, org_team_id: uuid.UUID) -> str:
    """Advance one queued batch to its end, from wherever it is.

    Returns the operation's terminal state. A retry after a killed attempt is
    the point of the job: each window of items commits its results and its
    cursor together, so a resume applies the items the killed attempt never
    reached and re-applies none that it did. An attempt that arrives after the
    batch already finished reports the terminal state instead of refusing -- a
    lost activity completion is not a reason to fail work that happened.
    """
    from alkera_core.files import bulk as bulk_core

    async with _session() as session:
        deps = await _deps(session, org_team_id)
        return await bulk_core.resume(deps.repo, deps.ctx, op_id, clock=deps.clock)


async def large_move(op_id: OperationId, org_team_id: uuid.UUID) -> str:
    """Advance one oversized subtree's move to its end, from wherever it is.

    Returns the operation's terminal state. A retry after a killed attempt is
    the point of the job: the runner reads its plan and its cursor off the
    operation row every time round, so it resumes at the batch boundary the
    crash left rather than rewriting the whole subtree. An attempt that arrives
    after the move already finished reports the terminal state instead of
    refusing -- a lost activity completion is not a reason to fail a move that
    happened.
    """
    from alkera_core.files.large_move import resume_large_move
    from alkera_core.files.ops import Operations

    async with _session() as session:
        deps = await _deps(session, org_team_id)
        state = await Operations(deps.repo, deps.ctx, deps.clock).get(op_id)
        if state.state in ("done", "cancelled"):
            log.info("files.large_move.already_finished", op_id=str(op_id), state=state.state)
            return state.state
        await resume_large_move(deps.repo, deps.ctx, op_id, clock=deps.clock)
        return (await Operations(deps.repo, deps.ctx, deps.clock).get(op_id)).state


async def copy(op_id: OperationId, org_team_id: uuid.UUID) -> str:
    """Advance one queued subtree copy to its end, from wherever it is.

    Returns the operation's terminal state. A retry after a killed attempt is
    the point of the job: the runner reads its plan and its cursor off the
    operation row every time round, so it resumes at the batch boundary the
    crash left rather than creating the whole subtree a second time. An attempt
    that arrives after the copy already finished reports the terminal state
    instead of refusing -- a lost activity completion is not a reason to fail a
    copy that happened.
    """
    from alkera_core.files.copy import resume_copy
    from alkera_core.files.ops import Operations

    async with _session() as session:
        deps = await _deps(session, org_team_id)
        state = await Operations(deps.repo, deps.ctx, deps.clock).get(op_id)
        if state.state in ("done", "cancelled"):
            log.info("files.copy.already_finished", op_id=str(op_id), state=state.state)
            return state.state
        await resume_copy(deps.repo, deps.ctx, op_id, clock=deps.clock)
        return (await Operations(deps.repo, deps.ctx, deps.clock).get(op_id)).state


async def acl_rewrite(op_id: OperationId, org_team_id: uuid.UUID) -> int:
    """Repair one batch of a subtree's ACL caches. Returns how many it fixed."""
    async with _session() as session:
        deps = await _deps(session, org_team_id)
        return await acl.rewrite(deps.repo, op_id)


RUNNER_FOR_KIND: dict[str, WorkflowType] = {
    "bulk": WorkflowType.FILES_BULK,
    "copy": WorkflowType.FILES_COPY,
    "move": WorkflowType.FILES_LARGE_MOVE,
    "upload": WorkflowType.FILES_PROMOTE,
}
"""Which workflow finishes an abandoned operation of each row ``kind``.

The row's own vocabulary, not the routes': an oversized move is a ``move`` row
run by ``files.large_move``, and an upload's commit is an ``upload`` row run by
``files.promote``, which reads the session ``complete`` bound onto the row.
"""

#: The one library entry point each recoverable kind is resumed through when
#: the deployment runs queued work in-process. The same functions above, which
#: are the same library calls the backend's inline dispatcher makes.
_INLINE_RUNNER: dict[str, Callable[[OperationId, uuid.UUID], Awaitable[str]]] = {
    "bulk": bulk,
    "copy": copy,
    "move": large_move,
    "upload": promote,
}

# The two halves of "this kind can be recovered" -- who runs it on a worker
# deployment and who runs it in-process -- checked at import, because a kind
# present in one and absent from the other is an abandoned row the pass picks
# up on every tick and can never finish.
assert RUNNER_FOR_KIND.keys() == _INLINE_RUNNER.keys(), (
    "a recoverable Files kind with a runner on only one deployment shape: "
    f"{sorted(RUNNER_FOR_KIND.keys() ^ _INLINE_RUNNER.keys())}"
)


@dataclass(frozen=True)
class QueuedHandoff:
    """One abandoned operation, and who should be asked to run it."""

    workflow: WorkflowType
    op_id: uuid.UUID
    org_team_id: uuid.UUID


@dataclass(frozen=True)
class RecoveryPass:
    """What one recovery tick found, re-handed, ran and gave up on."""

    at: datetime
    handoffs: tuple[QueuedHandoff, ...] = ()
    """Operations to hand back to a workflow. Empty on an inline deployment,
    which runs them itself."""
    ran: tuple[uuid.UUID, ...] = ()
    """Operations this pass drove to their end in-process."""
    failed: tuple[uuid.UUID, ...] = ()
    """Operations that ran out of offers and were failed with a reason."""
    unrecoverable: tuple[uuid.UUID, ...] = ()
    """Abandoned rows of a kind nothing can restart from the row alone."""


async def _orgs_with_abandoned(
    session: AsyncSession, cutoff: datetime, limit: int
) -> list[uuid.UUID]:
    """The orgs holding at least one ``queued`` row nobody ever claimed.

    Asked first so an idle fleet costs one indexed read per tick rather than a
    per-org repo, context and store for every tenant that has ever used Files.
    ``file_ops`` is a tenant table under a FORCE'd policy and this read has no
    tenant, so it goes through the cross-tenant seam: a login the policy binds
    is refused out loud rather than answering "nothing is abandoned" forever.
    """
    async with cross_tenant_read(session, reason="files.recover_queued.orgs") as read:
        rows = (
            await read.execute(
                text(
                    "SELECT DISTINCT org_team_id FROM file_ops "
                    "WHERE state = 'queued' AND heartbeat_at IS NULL "
                    "AND created_at <= CAST(:cutoff AS timestamptz) "
                    "ORDER BY org_team_id LIMIT :limit"
                ),
                {"cutoff": cutoff, "limit": limit},
            )
        ).fetchall()
    return [uuid.UUID(str(row[0])) for row in rows]


async def recover_queued(
    now: datetime | None = None,
    *,
    max_attempts: int | None = None,
    budget: int | None = None,
) -> RecoveryPass:
    """Find every abandoned ``queued`` Files operation and offer it a runner.

    The counterpart of the watchdog, which only judges ``running`` rows by
    their heartbeat. A route's hand-off is best-effort and bounded by two
    seconds — the orchestrator can be unreachable for exactly that long, or the
    process can be killed between answering 202 and the background task — and
    the row it leaves behind is ``queued`` with no heartbeat and nobody named
    to run it. Nothing else looks at those rows, so without this pass a bulk
    trash, a copy or an oversized move is stranded forever: never run, never
    failed, polled by a client that is never told.

    Finding a row is NOT deciding it was abandoned. A row whose runner exists
    but has not reached a worker slot looks identical from the database, so
    nothing is counted against a row here: the workflow offers each one to the
    orchestrator and :func:`settle_recovery` records what the orchestrator
    answered. Only a row the orchestrator had no runner for, every time, is
    eventually failed.

    ``budget`` is one fleet-wide bound on the rows a tick takes, not a bound
    per tenant: the pass is a net for a rare lost hand-off, and a per-org cap
    multiplied by an org cap is a tick whose real size is the product of two
    numbers nobody reads together.

    On a deployment that runs queued work in the process that queued it
    (``FILES_INLINE_OPERATIONS``) there is no orchestrator to ask, so the pass
    drives each operation through the very same library core the inline
    dispatcher calls, settles the attempt itself — a dispatch IS the hand-off
    there — and reports it under ``ran``.
    """
    at = now or datetime.now(UTC)
    config = get_settings()
    attempts = max_attempts if max_attempts is not None else config.files_queued_recovery_attempts
    remaining = budget if budget is not None else config.files_queued_recovery_budget
    if attempts < 1 or remaining < 1:
        raise ValueError(f"max_attempts and budget must be >= 1, got {attempts} and {remaining}")
    handoffs: list[QueuedHandoff] = []
    failed: list[uuid.UUID] = []
    unrecoverable: list[uuid.UUID] = []
    inline: list[tuple[str, OperationId, uuid.UUID]] = []
    async with _session() as session:
        orgs = await _orgs_with_abandoned(session, at - QUEUED_STALE_AFTER, remaining)
        for org_team_id in orgs:
            if remaining <= 0:
                break
            deps = await _deps(session, org_team_id)
            operations = Operations(deps.repo, deps.ctx, deps.clock)
            try:
                found = await operations.abandoned_queued(at, limit=remaining)
            except Exception:
                log.exception("files.recover_queued.org_failed", org_team_id=str(org_team_id))
                continue
            remaining -= len(found)
            for entry in found:
                if entry.kind not in RUNNER_FOR_KIND:
                    unrecoverable.append(uuid.UUID(str(entry.id)))
                    continue
                if config.files_inline_operations:
                    inline.append((entry.kind, entry.id, org_team_id))
                else:
                    handoffs.append(
                        QueuedHandoff(
                            workflow=RUNNER_FOR_KIND[entry.kind],
                            op_id=uuid.UUID(str(entry.id)),
                            org_team_id=org_team_id,
                        )
                    )
    ran = await _run_inline(inline, attempts, failed)
    log.info(
        "files.recover_queued.pass_done",
        offered=len(handoffs),
        ran=len(ran),
        failed=len(failed),
        unrecoverable=len(unrecoverable),
    )
    return RecoveryPass(
        at=at,
        handoffs=tuple(handoffs),
        ran=tuple(ran),
        failed=tuple(failed),
        unrecoverable=tuple(unrecoverable),
    )


async def _run_inline(
    inline: list[tuple[str, OperationId, uuid.UUID]],
    max_attempts: int,
    failed: list[uuid.UUID],
) -> list[uuid.UUID]:
    """Drive each abandoned operation here, and settle its attempt.

    Outside the session they were found on: each core opens its own, exactly as
    it does when a workflow calls it. Dispatching IS the hand-off on this
    shape, so the attempt is counted before the run — a core that dies taking
    the process with it must not leave the row looking never-offered.
    """
    ran: list[uuid.UUID] = []
    for kind, op_id, org_team_id in inline:
        settled = await settle_recovery({str(op_id): RUNNER_NUDGED}, org_team_id, max_attempts)
        failed.extend(settled)
        if uuid.UUID(str(op_id)) in settled:
            continue
        try:
            await _INLINE_RUNNER[kind](op_id, org_team_id)
        except Exception:
            # The failure is on the operation row already; the next tick offers
            # it again, and the attempt count is what eventually stops that.
            log.exception("files.recover_queued.inline_failed", op_id=str(op_id), kind=kind)
            continue
        ran.append(uuid.UUID(str(op_id)))
    return ran


async def settle_recovery(
    outcomes: dict[str, str], org_team_id: uuid.UUID, max_attempts: int | None = None
) -> tuple[uuid.UUID, ...]:
    """Record what the orchestrator answered for each offered row.

    ``nudged`` — there was no runner and a fresh one was taken — is what counts
    as an attempt; ``runner_present`` means the row was told about after all
    and its count goes back to zero, so a busy queue can never fail an
    operation that is about to run. Returns the operations whose runner was
    provably absent once too often and are now ``failed``.
    """
    attempts = (
        max_attempts if max_attempts is not None else get_settings().files_queued_recovery_attempts
    )
    async with _session() as session:
        deps = await _deps(session, org_team_id)
        operations = Operations(deps.repo, deps.ctx, deps.clock)
        recovery = await operations.settle_recovery(
            {
                OperationId(uuid.UUID(op_id)): cast(RecoveryOutcome, outcome)
                for op_id, outcome in outcomes.items()
            },
            max_attempts=attempts,
        )
    for entry in recovery.rehanded:
        log.info(
            "files.recover_queued.offered",
            op_id=str(entry.id),
            kind=entry.kind,
            attempt=entry.attempts,
            org_team_id=str(org_team_id),
        )
    for op_id_ in recovery.failed:
        log.warning(
            "files.recover_queued.abandoned",
            op_id=str(op_id_),
            org_team_id=str(org_team_id),
            attempts=attempts,
        )
    return tuple(uuid.UUID(str(op_id_)) for op_id_ in recovery.failed)


__all__ = [
    "GC_QUEUE",
    "JANITOR_QUEUE",
    "RUNNER_FOR_KIND",
    "AdoptReport",
    "DepsFactory",
    "FilesJobDeps",
    "FilesJobsNotWiredError",
    "GcPass",
    "QueuedHandoff",
    "RecoveryPass",
    "acl_rewrite",
    "copy",
    "files_gc",
    "files_gc_adopt",
    "janitor",
    "known_domains",
    "known_rows",
    "large_move",
    "orgs_with_drives",
    "promote",
    "recover_queued",
    "reset_deps_factory",
    "reset_store_factory",
    "set_deps_factory",
    "set_store_factory",
    "settle_recovery",
]
