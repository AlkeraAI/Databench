"""The one object a Files route holds, and how it is built.

A route resolves nothing itself: it declares ``FilesCtx``
(``backend.api.deps.files_context``) and gets a ``FilesContext`` whose repo is
already scoped to the caller's org, whose store handle cannot address another
org's prefix, and whose drive exists because the builder ensured it on first
touch. Everything below this line is wiring; the decisions all live in
``alkera_core.files``.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime

from alkera_core.authz.principal import ActingContext
from alkera_core.compute.workspace_lease import holds_work_in
from alkera_core.config import Settings
from alkera_core.config import settings as default_settings
from alkera_core.db.cross_tenant import cross_tenant_write
from alkera_core.files import drives
from alkera_core.files.clock import Clock, SystemClock
from alkera_core.files.errors import InvalidRequest, NotFound
from alkera_core.files.ids import DomainId, DriveId, NodeId, OrgScope
from alkera_core.files.leases import HolderIdentity, holder_identity
from alkera_core.files.ownership import store_identity, write_marker
from alkera_core.files.quota import CeilingsResolver
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.scoped import DomainStore
from alkera_core.logging import get_logger
from alkera_core.models.files.stores import FileDrive, FileStore
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services import org as org_services
from backend.services.files.facts import verified_machine_id
from backend.services.files.store import store_factory

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class FilesContext:
    """Everything a Files call needs, resolved once per request."""

    repo: FilesRepo
    ctx: ActingContext
    clock: Clock
    store: DomainStore
    settings: Settings
    drive: FileDrive
    #: The ceilings a write is checked against, resolved as the application
    #: before the Files transaction; ``None`` on a read, which never asks.
    ceilings: CeilingsResolver | None = None
    #: The machine this request PROVED it is, and ``None`` for everyone else —
    #: a person, and an agent whose assertion nobody verified. Resolved once,
    #: here, because it is what the lease layer derives its fencing identity
    #: from: a box's machine id is public, so an assertion carries holdership
    #: only once it has been checked against the registration.
    agent_machine_id: str | None = None

    @property
    def holder(self) -> HolderIdentity | None:
        """This request's fencing identity: the credential's own principal, or
        a PROVEN machine, or nothing at all.

        Every lease statement and every fenced write of this request compares
        this one value, so a route cannot accidentally fence on something the
        request chose.
        """
        return holder_identity(self.ctx, verified_machine_id=self.agent_machine_id)


def stamp_new_domain(
    store_row_id: uuid.UUID,
    settings: Settings,
    session: AsyncSession,
    *,
    clock: Clock | None = None,
) -> drives.DomainCreated:
    """The hook that marks a brand-new dedup domain as this deployment's.

    The marker is what the `files.gc` collector reads before it will consider a
    prefix at all, and it names the deployment by its ``file_stores`` row id.

    Nothing in the request has any use for it, so nothing in the request waits
    long for it. The write gets ``files_store_stamp_timeout_seconds`` and no
    more: a store that refuses the connection, or accepts it and never answers,
    costs the deadline rather than the driver's own connect-and-retry ladder --
    which is seconds per new org, paid by signup, org creation and the first
    chat of an org's life, on a call the store's reachability has no business
    being in.

    Whether the marker landed is recorded on the domain row. A domain left
    unmarked is the safe state -- the collector reports it and never collects
    it -- and ``owner_markers``, the janitor's sweeper, settles it on the next
    pass; ``python -m worker files gc-adopt`` is still how an operator adopts a
    prefix no row names.
    """
    the_clock = clock or SystemClock()

    async def stamp(domain_id: uuid.UUID) -> None:
        # OUTSIDE the deadline, and on purpose: this builds the driver (and, on
        # the first Files call of a process, resolves the credentials behind
        # it) from settings, with no call to the store. Inside, a cold client
        # would spend the whole budget being constructed and leave the very
        # first domain of every process owed for a reason that has nothing to
        # do with whether the store is reachable.
        factory = store_factory(settings, clock=the_clock)
        try:
            async with asyncio.timeout(settings.files_store_stamp_timeout_seconds):
                store = await factory.for_domain(DomainId(domain_id))
                await write_marker(store, str(store_row_id), now=the_clock.now())
        except Exception as exc:
            # The reason, not a traceback: a store that is down fails this on
            # every org a deployment makes, and rendering one stack per signup
            # costs more than the write it is reporting on.
            log.warning(
                "files.ownership.stamp_failed",
                domain_id=str(domain_id),
                error=type(exc).__name__,
            )
            return
        await mark_domain_stamped(session, domain_id, at=the_clock.now())

    return stamp


async def mark_domain_stamped(session: AsyncSession, domain_id: uuid.UUID, *, at: datetime) -> None:
    """Record that ``domain_id``'s prefix carries this deployment's marker.

    Written on the session the caller is already using, so it lives or dies
    with the rows the marker was written for: a creation that rolls back leaves
    the column NULL against a marker that is already in the store, and the
    sweeper reads that prefix once, finds the marker is ours, and records it
    without writing anything.
    """
    await session.execute(
        text("UPDATE dedup_domains SET owner_marked_at = :at WHERE id = :id"),
        {"at": at, "id": domain_id},
    )


async def ensure_store_row(session: AsyncSession, settings: Settings) -> FileStore:
    """The ``file_stores`` row for the configured backend, created on first use.

    The row is platform-wide (a bucket is not a tenant's), so it is keyed by the
    four things that identify a bucket rather than by the org. A concurrent
    first request loses the unique race and reads the winner's row instead of
    failing the request.
    """
    # The row records the DRIVER, not the configured provider: `aws` and
    # `s3_compatible` are both addressed as `s3` (the column's catalogue), and
    # `settings.files_store_driver` is the single place that translation lives.
    identity = store_identity(settings)
    driver, bucket = identity["driver"], identity["bucket"]
    endpoint, region = identity["endpoint"], identity["region"]
    stmt = FilesRepo.select_stores().where(
        FileStore.driver == driver,
        FileStore.bucket == bucket,
        FileStore.endpoint == endpoint,
        FileStore.region == region,
    )
    existing = (await session.execute(stmt)).scalars().first()
    if existing is not None:
        return existing
    row = FileStore(
        id=uuid.uuid4(),
        driver=driver,
        bucket=bucket,
        endpoint=endpoint,
        region=region,
        capabilities={},
        transfer_modes=["single", "proxied"],
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        found = (await session.execute(stmt)).scalars().first()
        if found is None:
            raise
        return found
    return row


async def _machine_drive(
    db: AsyncSession,
    principal: ActingContext,
    *,
    drive_id: uuid.UUID | None,
    org_id: uuid.UUID | None,
) -> FileDrive:
    """The drive a box on its machine credential addressed, or the opaque
    not-found.

    A box has no drive of its own: it serves chats in orgs that are never its
    operator org, so "the caller's drive" names nothing, and a request that
    names no drive — by id in its path, or by org in a signed content claim —
    is refused before anything is read. The drive named is looked up across
    orgs and admitted only when the credential SERVES its org and the box
    STANDS in it (:func:`_stands_in`); a drive of any other org is the same
    not-found a nonexistent id gets, so a box learns nothing about drives in
    orgs where it holds no work, whichever route or node named them. Nothing is
    created on the way: a machine never provisions an org's drive.
    """
    if drive_id is None and org_id is None:
        raise NotFound()
    async with cross_tenant_write(db, reason="files.machine.drive_lookup"):
        if drive_id is not None:
            drive = await FilesRepo.drive_anywhere(db, DriveId(drive_id))
        elif org_id is not None:
            drive = await FilesRepo.org_drive_anywhere(db, org_id)
        admitted = (
            drive is not None
            and principal.serves(drive.org_team_id)
            and await _stands_in(db, principal, uuid.UUID(str(drive.org_team_id)))
        )
    if drive is None or not admitted:
        raise NotFound()
    return drive


async def _stands_in(db: AsyncSession, principal: ActingContext, org_id: uuid.UUID) -> bool:
    """Whether the box has standing in ``org_id``, past the ceiling ``serves``
    already checked. A pool box serves every org only in that it may be placed
    anywhere, so in an org other than its operator's it stands only where it
    holds work or is finishing on a lease (:func:`holds_work_in`), the same
    orgs its own session is held to. Anywhere else a drive that exists and one
    that does not are one answer, before any route reads a row of it.
    A dedicated box's served orgs and an org-bound worker's one org are their
    standing already."""
    if not principal.serves_every_org or org_id == principal.org_id:
        return True
    return await holds_work_in(db, machine_id=principal.acting_principal.id, org_id=org_id)


async def node_scope(db: AsyncSession, principal: ActingContext, node_id: NodeId) -> OrgScope:
    """The org scope a Files read of ``node_id`` by ``principal`` runs in.

    A person's or an agent's is their own org. A box on its machine
    credential has none of its own to read in: the node is looked up across
    orgs for its drive, and the drive is admitted exactly as a route admits
    the drive a box names (:func:`_machine_drive`), so the scope is the
    drive's org only when the credential serves it. A node that is not there
    and one in an org the box does not serve are the same :class:`NotFound`.

    It names where to read and fences nothing: serving an org is the ceiling
    of a box's reach, not a grant inside it. What the box may do with the
    node is still the Files policy's (the binding of the chat or workspace
    it runs, and the lease it proves it holds), decided on the repo this
    scope opens.
    """
    if not principal.is_machine:
        if principal.org_id is None:
            raise NotFound()
        return OrgScope(org_team_id=principal.org_id)
    async with cross_tenant_write(db, reason="files.machine.node_lookup"):
        drive_id = await FilesRepo.drive_of_node_anywhere(db, node_id)
    if drive_id is None:
        raise NotFound()
    drive = await _machine_drive(db, principal, drive_id=drive_id, org_id=None)
    return OrgScope(org_team_id=uuid.UUID(str(drive.org_team_id)))


async def build_files_context(
    db: AsyncSession,
    principal: ActingContext,
    *,
    settings: Settings | None = None,
    clock: Clock | None = None,
    resolve_ceilings: bool = False,
    drive_id: uuid.UUID | None = None,
    org_id: uuid.UUID | None = None,
) -> FilesContext:
    """Build the context for ``principal``: scope, repo, drive, store handle.

    A person's context is their org's drive, ensured on first touch. A box on
    its machine credential has none: its context is the drive it NAMED —
    ``drive_id`` from the request path (or, on the drive route, the drive of
    the chat it names), or ``org_id`` from a signed content claim — admitted
    only when the credential serves that org, and never created here
    (:func:`_machine_drive`). Both are ignored for a person, whose drive the
    credential decides.

    ``resolve_ceilings`` installs the resolver a write consults for the org's
    override / plan and the caller's own limits. It reads lazily, as the
    application on a session of its own — the Files role cannot read ``teams``
    or the billing tables — so a request that never adds usage never pays. A
    box's write is held to the org's ceiling alone: nobody's own limit is its.
    """
    resolved = settings or default_settings
    the_clock = clock or SystemClock()
    if principal.org_id is None:
        raise InvalidRequest("files.no_org", "This principal is not scoped to an org")
    store_row = await ensure_store_row(db, resolved)
    if principal.is_machine:
        drive = await _machine_drive(db, principal, drive_id=drive_id, org_id=org_id)
        scope_org = uuid.UUID(str(drive.org_team_id))
        repo = FilesRepo(db, OrgScope(org_team_id=scope_org))
    else:
        scope_org = principal.org_id
        repo = FilesRepo(db, OrgScope(org_team_id=scope_org))
        async with repo.transaction():
            drive = await drives.ensure_org_drive(
                repo,
                principal,
                scope_org,
                store_id=store_row.id,
                on_domain_created=stamp_new_domain(store_row.id, resolved, db, clock=the_clock),
            )
    store = await store_factory(resolved, clock=the_clock).for_domain(
        DomainId(drive.dedup_domain_id)
    )
    ceilings = write_ceilings(principal, drive) if resolve_ceilings else None
    return FilesContext(
        repo=repo,
        ctx=principal,
        clock=the_clock,
        store=store,
        settings=resolved,
        drive=drive,
        ceilings=ceilings,
        # Resolved before any Files transaction opens, as the platform: the
        # registration lives in a table the Files role cannot see, and every
        # door into Files goes through this builder, so no route can reach a
        # fence without the answer.
        agent_machine_id=await verified_machine_id(db, principal),
    )


def write_ceilings(principal: ActingContext, drive: FileDrive) -> CeilingsResolver | None:
    """The storage ceilings a write by ``principal`` into ``drive`` is held
    to: the org's override or plan, and the person's own limits (a box's
    write is held to the org's alone). ``None`` for a principal nothing can
    be charged to. Resolved lazily, as the application on a session of its
    own, because the Files role cannot read the billing tables."""
    if not principal.is_machine and principal.effective_user_id is None:
        return None
    return org_services.ceilings_resolver(
        org_id=uuid.UUID(str(drive.org_team_id)),
        user_id=principal.effective_user_id,
        drive=drive,
    )


async def restart_attempt(files: FilesContext) -> None:
    """Roll a losing attempt back and reload the rows the context carries.

    A replayed attempt runs on the request's own session, and the rollback that
    clears the loser's claim also expires every instance that session loaded —
    including the drive this context was built with before the first attempt
    began. The drive is read synchronously all over a handler (a caller's facts,
    the ceilings a write consults, the drive id a route stamps), and an expired
    instance answers such a read with a refresh the async session cannot run
    there, so every replay would fail on its first touch. The drive is reloaded
    here instead, by the identity it already has, inside the attempt that is
    about to run: in place, so every holder of the instance — the context, the
    ceilings resolver built around it — reads the fresh row without IO.

    The read is the drive's own Files read, under the Files role and the org
    the context is scoped to. A drive that is gone by the time the attempt
    reruns is the opaque not-found any other missing drive earns.
    """
    session = files.repo.session
    # The identity, not the column: it is held on the instance's state and
    # survives the expiry, where reading ``drive.id`` would itself be the IO.
    identity = sa_inspect(files.drive).identity
    if identity is None:
        raise RuntimeError("a Files context's drive is always a persisted row")
    await session.rollback()
    repo = FilesRepo.joined(session, files.repo.scope)
    async with repo.transaction():
        found = await repo.reload_drive(DriveId(identity[0]))
    if found is None:
        raise NotFound()


__all__ = [
    "FilesContext",
    "build_files_context",
    "ensure_store_row",
    "mark_domain_stamped",
    "node_scope",
    "restart_attempt",
    "stamp_new_domain",
    "write_ceilings",
]
