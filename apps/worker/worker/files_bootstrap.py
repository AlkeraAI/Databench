"""Build the Files jobs' world from settings, once, at worker boot.

``worker.tasks.files`` refuses to run unwired: which principal a pass acts as
and which bucket it writes to are deployment decisions, and the cores are not
entitled to invent either. This module is where the deployment answers, and it
answers from settings alone.

A deployment that does not serve Files (``FILES_ENABLED=false``) leaves the
family unwired on purpose: the worker still boots and still serves every other
family, and a Files activity that somehow reaches it refuses with
``FilesJobsNotWiredError`` rather than sweeping a store nobody configured.

A deployment that *does* serve Files but names a store this process cannot
build is refused **here**, at boot, with the setting's own name in the message
— not five minutes later on the first janitor tick, where it would surface as
an activity failure in a workflow history nobody is watching.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Final

from alkera_core.authz.enums import PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.config import Settings
from alkera_core.config import settings as process_settings
from alkera_core.files import stats
from alkera_core.files.clock import Clock, SystemClock
from alkera_core.files.gc import Janitor
from alkera_core.files.ids import DomainId, OrgScope
from alkera_core.files.ops import Operations
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.aws import AwsStore
from alkera_core.files.store.s3_compatible import S3CompatibleStore, S3Config
from alkera_core.files.store.scoped import (
    AwsScoped,
    DomainStore,
    FilesystemScoped,
    S3CompatConfig,
    S3CompatScoped,
    ScopedStoreFactory,
)
from alkera_core.files.sweepers import (
    DomainBoundAdmin,
    ReachabilityTarget,
    SweepDeps,
)
from alkera_core.files.trash import Trash
from alkera_core.files.uploads import UploadCompletion
from alkera_core.logging import get_logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from worker.tasks.files import (
    DepsFactory,
    DomainMarkerRepair,
    FilesJobDeps,
    set_deps_factory,
    set_store_factory,
)

log = get_logger(__name__)

JANITOR_PRINCIPAL_ID: Final = "00000000-0000-4000-8000-0000000f11e5"
"""The platform's own id, stable across processes so every housekeeping row in
every org names one actor. Deliberately not a user: no human asked for a sweep,
and an audit trail that blamed one would be a lie."""

JANITOR_LABEL: Final = "files-janitor"


class FilesSettingsError(RuntimeError):
    """The Files settings name a store this process cannot build."""


def janitor_context(org_team_id: uuid.UUID) -> ActingContext:
    """The context a Files job acts under in ``org_team_id``.

    A SERVICE principal presenting no credential of its own: the worker is the
    platform, it authenticated nothing, and the org id is what keeps every row
    it touches inside the tenant whose pass is running.
    """
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.SERVICE,
            id=JANITOR_PRINCIPAL_ID,
            org_id=org_team_id,
            label=JANITOR_LABEL,
            credential=None,
        )
    )


def build_store_factory(config: Settings, *, clock: Clock) -> ScopedStoreFactory:
    """The scoped store factory ``FILES_STORE_PROVIDER`` names.

    Raises :class:`FilesSettingsError` naming the missing setting rather than
    constructing a driver that would fail on its first call.
    """
    provider = config.files_store_provider
    if provider == "filesystem":
        return FilesystemScoped(config.files_store_root_path, clock=clock)

    missing: list[str] = []
    if not config.files_store_bucket:
        missing.append("FILES_STORE_BUCKET")
    if provider == "s3_compatible" and not config.files_store_endpoint:
        missing.append("FILES_STORE_ENDPOINT")
    if provider == "aws" and not config.files_vend_role_arn:
        missing.append("FILES_VEND_ROLE_ARN")
    if missing:
        raise FilesSettingsError(
            f"FILES_STORE_PROVIDER={provider} needs {', '.join(missing)}; "
            "the Files worker refuses to boot with a store it cannot address"
        )

    secret = config.files_store_secret_key
    s3 = S3Config(
        endpoint_url=config.files_store_endpoint,
        region=config.files_store_region,
        bucket=config.files_store_bucket or "",
        access_key=config.files_store_access_key,
        secret_key=secret.get_secret_value() if secret is not None else None,
        addressing=config.files_store_addressing,
        vend_role_arn=config.files_vend_role_arn,
    )
    if provider == "aws":
        return AwsScoped(AwsStore(s3, clock=clock), clock=clock)
    inner = S3CompatibleStore(s3, clock=clock, layout="bucket")
    return S3CompatScoped(S3CompatConfig(store=inner), clock=clock)


async def domain_of(session: AsyncSession, org_team_id: uuid.UUID) -> DomainId | None:
    """The dedup domain the org's bytes live under, or ``None`` before its
    drive exists — the store handle is bound to the domain, never to the org."""
    row = (
        await session.execute(
            text(
                "SELECT dedup_domain_id FROM file_drives "
                "WHERE org_team_id = :org ORDER BY id LIMIT 1"
            ),
            {"org": org_team_id},
        )
    ).first()
    return None if row is None else DomainId(uuid.UUID(str(row[0])))


def _sweep_deps(
    session: AsyncSession,
    store_factory: ScopedStoreFactory,
    *,
    repo: FilesRepo,
    ctx: ActingContext,
    clock: Clock,
    domain_id: DomainId,
    store: DomainStore,
    grant_delay: timedelta,
    unsynced_grace: timedelta,
) -> SweepDeps:
    """The sweepers' world, built from the services that own the behaviour.

    Every delegating sweeper takes a bound method rather than a re-implementation:
    a sweeper that expires an upload session runs the upload service's own
    expiry, so the state machine has one implementation and the janitor cannot
    drift from the request path. The two lambdas supply only the domain, which
    the org-scoped sweeper has no way to know.

    ``incoming`` and ``multipart`` take the factory's bucket-wide admin handle,
    which is the only handle that can name a staged object no row points at.
    The reachability sweep takes it too, as its object-age source: the horizon
    can then see that an object post-dates the sweep's own start and keep it
    for the writer that is still finishing it.

    The handle is bucket-rooted, so it is bound to the domain before the
    sweepers see it — they read the session id out of a domain-relative
    ``incoming/<session>/…`` and an unbound handle would make every staged
    object read session-less.
    """
    admin = store_factory.admin()
    janitor = Janitor(
        lambda scope: FilesRepo(session, scope),
        store_factory,
        clock,
        age_source=admin,
    )
    domain_admin = DomainBoundAdmin(admin, domain_id)
    return SweepDeps(
        repo=repo,
        ctx=ctx,
        clock=clock,
        incoming=domain_admin,
        multipart=domain_admin,
        expire_deleted=lambda now: janitor.expire_deleted(domain_id, now),
        sweep_expired_sessions=UploadCompletion(repo, ctx, clock, store).sweep_expired,
        watchdog=Operations(repo, ctx, clock, store).watchdog,
        purge_trash=Trash(repo, ctx, clock, store).purge,
        aggregate_stats=lambda: stats.aggregate([repo]),
        lease_grant_delay=grant_delay,
        unsynced_grace=unsynced_grace,
        reachability=ReachabilityTarget(janitor=janitor, domain_id=domain_id),
        # Bucket-wide, like the adopt command: the marker sits at
        # ``domains/<id>/meta/owner.json``, outside every prefix a domain-bound
        # handle addresses. One instance per org per pass, which is what makes
        # the deployment id one read rather than one per domain.
        stamp_domain=DomainMarkerRepair(session, store_factory),
    )


def deps_factory(
    store_factory: ScopedStoreFactory,
    *,
    clock: Clock,
    grant_delay: timedelta = timedelta(seconds=60),
    unsynced_grace: timedelta = timedelta(days=1),
) -> DepsFactory:
    """A factory that builds one org's world on the session it is handed."""

    async def build(session: AsyncSession, org_team_id: uuid.UUID) -> FilesJobDeps:
        domain_id = await domain_of(session, org_team_id)
        store = None if domain_id is None else await store_factory.for_domain(domain_id)
        repo = FilesRepo(session, OrgScope(org_team_id=org_team_id))
        ctx = janitor_context(org_team_id)
        sweep = (
            None
            if domain_id is None or store is None
            else _sweep_deps(
                session,
                store_factory,
                repo=repo,
                ctx=ctx,
                clock=clock,
                domain_id=domain_id,
                store=store,
                grant_delay=grant_delay,
                unsynced_grace=unsynced_grace,
            )
        )
        return FilesJobDeps(repo=repo, ctx=ctx, clock=clock, store=store, sweep=sweep)

    return build


def wire_files_jobs(config: Settings | None = None, *, clock: Clock | None = None) -> bool:
    """Install the Files deps factory when the deployment serves Files.

    Returns whether the family was wired. Raises :class:`FilesSettingsError`
    when Files is on but its store settings are incomplete.
    """
    resolved = process_settings if config is None else config
    if not resolved.files_enabled:
        log.info("worker.files_not_configured")
        return False
    the_clock = clock or SystemClock()
    store_factory = build_store_factory(resolved, clock=the_clock)
    set_deps_factory(
        deps_factory(
            store_factory,
            clock=the_clock,
            grant_delay=timedelta(seconds=resolved.files_lease_grant_delay_seconds),
            unsynced_grace=timedelta(seconds=resolved.files_unsynced_grace_seconds),
        )
    )
    # The collector walks the bucket rather than the orgs, so it takes the
    # factory itself: the same one the per-org deps are built from, so a
    # deployment can never have the two pointed at different stores.
    set_store_factory(store_factory)
    log.info("worker.files_wired", provider=resolved.files_store_provider)
    return True


__all__ = [
    "JANITOR_LABEL",
    "JANITOR_PRINCIPAL_ID",
    "FilesSettingsError",
    "build_store_factory",
    "deps_factory",
    "domain_of",
    "janitor_context",
    "wire_files_jobs",
]
