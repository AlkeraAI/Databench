"""``alkera files admin …`` — the operator verbs.

These are not the user verbs. ``push``/``pull``/``mount`` speak to the API as a
person; the verbs here run **in process** against ``DATABASE_URL`` and the store
settings, the way alembic does, because every one of them is a repair or an
audit tool that has to work when the API is the thing that is broken. They build
a :class:`~alkera_core.files.gc.Janitor` from settings and call the same library
the nightly ``files.janitor`` schedule calls, so what an operator sees at 3 a.m.
is what the scheduled sweep did.

The sub-app is hidden: reachable by name, never advertised beside the verbs a
user is meant to type.

Exit codes are the contract a runbook and a cron line read:

``0``
    clean — nothing to act on.
``1``
    findings — the command worked and the deployment has a problem (a scrub
    mismatch, an fsck finding, a verify disagreement, a non-empty quarantine, a
    tripped GC breaker).
``2``
    the command could not run: a bad argument, an unknown reference, a row the
    migrations were supposed to create.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Final

import typer
from alkera_core.config import settings
from alkera_core.files.clock import SystemClock
from alkera_core.files.fsck import FsckReport, run_fsck
from alkera_core.files.gc import Janitor, SweepPlan
from alkera_core.files.hashing import StreamHasher
from alkera_core.files.ids import DomainId, OrgScope
from alkera_core.files.repo import FilesRepo
from alkera_core.files.scrub import ScrubResult, run_scrub
from alkera_core.files.store.aws import AwsStore
from alkera_core.files.store.keys import DOMAIN_PREFIX
from alkera_core.files.store.s3_compatible import S3CompatibleStore, S3Config
from alkera_core.files.store.scoped import (
    AwsScoped,
    FilesystemScoped,
    S3CompatConfig,
    S3CompatScoped,
    ScopedStoreFactory,
)
from rich.console import Console
from rich.table import Table
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

#: Nothing to act on.
EXIT_OK: Final = 0
#: The command ran and found something an operator has to decide about.
EXIT_FINDINGS: Final = 1
#: The command could not run at all.
EXIT_USAGE: Final = 2

#: The keys a store uses to say how an object is encrypted at rest.
ENCRYPTION_KEYS: Final = ("sse", "sse_algorithm", "kms_key_id", "ssekms_key_id")

admin_app = typer.Typer(
    name="admin",
    help="Operator verbs for the Files store. Runs in-process against DATABASE_URL.",
    hidden=True,
    no_args_is_help=True,
)
quarantine_app = typer.Typer(name="quarantine", help="Triage the quarantine queue.")
admin_app.add_typer(quarantine_app)

console = Console()

OrgOpt = Annotated[str | None, typer.Option("--org", help="Limit to one org's domains.")]


@dataclass(frozen=True, slots=True)
class Target:
    """One dedup domain and the org that owns it — the janitor's unit of work.

    The domain-to-org map is platform data an org-scoped repo cannot read, so
    every operator verb resolves it once up front, exactly as the scheduled
    sweep's caller does before it hands the pair to the janitor.
    """

    org: OrgScope
    domain_id: DomainId


def build_store_factory() -> ScopedStoreFactory:
    """The scoped store factory the settings describe.

    A factory rather than a store: widening a per-domain handle into one that
    can name ``domains/<id>/…`` across domains is what
    :meth:`ScopedStoreFactory.admin` is for, and the janitor is one of the few
    callers allowed to ask. Handing it a bucket-rooted ``ObjectStore`` directly
    would put that widening back on this module.

    Built here rather than imported from the worker's bootstrap: ``alkera-cli``
    does not depend on ``alkera-worker``, and an operator verb has to run on a
    box where only the CLI is installed.
    """
    clock = SystemClock()
    provider = settings.files_store_provider
    if provider == "filesystem":
        return FilesystemScoped(settings.files_store_root_path, clock=clock)
    secret = settings.files_store_secret_key
    config = S3Config(
        endpoint_url=settings.files_store_endpoint,
        region=settings.files_store_region,
        bucket=settings.files_store_bucket or "",
        access_key=settings.files_store_access_key,
        secret_key=secret.get_secret_value() if secret is not None else None,
        addressing=settings.files_store_addressing,
        vend_role_arn=settings.files_vend_role_arn,
    )
    if provider == "aws":
        return AwsScoped(AwsStore(config, clock=clock), clock=clock)
    inner = S3CompatibleStore(config, clock=clock, layout="bucket")
    return S3CompatScoped(S3CompatConfig(store=inner), clock=clock)


@asynccontextmanager
async def operator() -> AsyncIterator[tuple[AsyncSession, Janitor]]:
    """A session on ``DATABASE_URL`` and a janitor built from settings.

    One engine per invocation, disposed on the way out: these are one-shot
    commands, and a leaked pool holds a Postgres connection open long after the
    process has printed its table.
    """
    engine = create_async_engine(settings.database_url, pool_size=1, max_overflow=0)
    session = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        factory = build_store_factory()
        janitor = Janitor(
            lambda scope: FilesRepo(session, scope),
            factory,
            SystemClock(),
            # The bucket-wide handle is also how an object's age is read, and
            # without it every deadline that compares an object's write time
            # against a window is silently never met: `fsck` could not report a
            # parked object past the deleted window at all.
            age_source=factory.admin(),
            holder="alkera-files-admin",
        )
        yield session, janitor
    finally:
        await session.close()
        await engine.dispose()


async def _targets(session: AsyncSession, org: str | None) -> list[Target]:
    """Every (org, domain) pair, or only the named org's.

    Read through the plain session rather than the scoped repo: ``dedup_domains``
    is where the map from a domain to its owner lives, and an org-scoped repo
    would have to know the answer already in order to ask.
    """
    statement = (
        "SELECT org_team_id, id FROM dedup_domains ORDER BY id"
        if org is None
        else "SELECT org_team_id, id FROM dedup_domains "
        "WHERE org_team_id = CAST(:org AS uuid) ORDER BY id"
    )
    rows = (await session.execute(text(statement), _org_params(org))).all()
    return [
        Target(org=OrgScope(org_team_id=org_id), domain_id=DomainId(domain_id))
        for org_id, domain_id in rows
    ]


def _org_params(org: str | None) -> dict[str, Any]:
    return {} if org is None else {"org": org}


def _org_uuid(value: str | None) -> str | None:
    """Validate ``--org`` before it reaches a query, so a typo exits 2."""
    if value is None:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        console.print(f"[red]not a uuid:[/red] {value}")
        raise typer.Exit(EXIT_USAGE) from None


def _run(main: Callable[[], Coroutine[Any, Any, int]]) -> None:
    """Run one verb's coroutine and make its exit code the process's."""
    raise typer.Exit(asyncio.run(main()))


@admin_app.command("scrub")
def scrub_command(
    org: OrgOpt = None,
    sample: Annotated[int, typer.Option("--sample", help="Percent of objects to read.")] = 5,
    full: Annotated[bool, typer.Option("--full", help="Read every object (sample 100).")] = False,
    budget_mb: Annotated[int | None, typer.Option("--budget-mb", help="Cap bytes read.")] = None,
) -> None:
    """Re-read stored bytes and prove they still hash the way the row says."""
    scope = _org_uuid(org)
    percent = 100 if full else sample
    if not 0 <= percent <= 100:
        console.print("[red]--sample must be between 0 and 100[/red]")
        raise typer.Exit(EXIT_USAGE)

    async def main() -> int:
        async with operator() as (session, janitor):
            results: list[ScrubResult] = []
            for target in await _targets(session, scope):
                result = await run_scrub(
                    janitor,
                    org=target.org,
                    domain_id=target.domain_id,
                    sample_pct=percent,
                    budget_bytes=None if budget_mb is None else budget_mb * 1024 * 1024,
                )
                # The scrub itself queues each finding for an operator — it can
                # never tell which side of a mismatch is wrong, so nothing here
                # repairs and nothing here re-files what the library filed.
                results.append(result)
            return _print_scrub(results)

    _run(main)


def _print_scrub(results: Sequence[ScrubResult]) -> int:
    table = Table(title="files scrub")
    for column in ("domain", "checked", "sampled", "bytes read", "findings"):
        table.add_column(column)
    findings = 0
    for result in results:
        findings += len(result.findings)
        table.add_row(
            str(result.domain_id),
            str(result.checked),
            str(result.sampled),
            str(result.bytes_read),
            str(len(result.findings)),
        )
    console.print(table)
    for result in results:
        for finding in result.findings:
            console.print(f"{finding.reason} version={finding.version_id}")
    return EXIT_FINDINGS if findings else EXIT_OK


@admin_app.command("fsck")
def fsck_command(
    org: OrgOpt = None,
    repair_safe: Annotated[
        bool, typer.Option("--repair-safe", help="Fix only the derivable findings.")
    ] = False,
) -> None:
    """Cross-check the store against the metadata; repair only what is safe."""
    scope = _org_uuid(org)

    async def main() -> int:
        async with operator() as (session, janitor):
            reports: list[FsckReport] = []
            for target in await _targets(session, scope):
                reports.append(
                    await run_fsck(
                        janitor,
                        org=target.org,
                        domain_id=target.domain_id,
                        repair_safe=repair_safe,
                    )
                )
            return _print_fsck(reports)

    _run(main)


def _print_fsck(reports: Sequence[FsckReport]) -> int:
    table = Table(title="files fsck")
    for column in ("domain", "code", "kind", "ref", "repaired"):
        table.add_column(column)
    total = 0
    for report in reports:
        repaired = {finding.ref_id for finding in report.repaired}
        for finding in report.findings:
            total += 1
            table.add_row(
                str(report.domain_id),
                finding.code,
                finding.kind,
                finding.ref_id,
                "yes" if finding.ref_id in repaired else "no",
            )
    console.print(table)
    return EXIT_FINDINGS if total else EXIT_OK


@admin_app.command("verify")
def verify_command(
    node: Annotated[str, typer.Argument(help="Node id whose head version is re-hashed.")],
) -> None:
    """Re-hash one node's head object and compare it with the row."""

    async def main() -> int:
        async with operator() as (session, janitor):
            row = (
                await session.execute(
                    text(
                        "SELECT v.id, v.store_key, v.content_hash, v.size_bytes, "
                        "d.dedup_domain_id FROM file_nodes n "
                        "JOIN file_versions v ON v.id = n.head_version_id "
                        "JOIN file_drives d ON d.id = n.drive_id "
                        "WHERE n.id = CAST(:node AS uuid)"
                    ),
                    {"node": node},
                )
            ).first()
            if row is None:
                console.print(f"[red]no node with a head version:[/red] {node}")
                return EXIT_USAGE
            absolute = f"{DOMAIN_PREFIX}{row.dedup_domain_id}/{row.store_key}"
            hasher = StreamHasher()
            async for chunk in await janitor._store.get(absolute):
                hasher.update(chunk)
            digests = hasher.finalize()
            recomputed = digests.content_hash.hex()
            matched = recomputed == row.content_hash and digests.size == int(row.size_bytes)
            table = Table(title="files verify")
            table.add_column("field")
            table.add_column("value")
            table.add_row("node", node)
            table.add_row("version", str(row.id))
            table.add_row("store key", absolute)
            table.add_row("recorded hash", str(row.content_hash))
            table.add_row("recomputed hash", recomputed)
            table.add_row("recorded size", str(row.size_bytes))
            table.add_row("read size", str(digests.size))
            table.add_row("verdict", "match" if matched else "mismatch")
            console.print(table)
            return EXIT_OK if matched else EXIT_FINDINGS

    _run(main)


@admin_app.command("gc")
def gc_command(
    org: OrgOpt = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run/--no-dry-run", help="Print the plan without moving bytes.")
    ] = True,
) -> None:
    """Print the sweep plan. This verb never moves an object itself."""
    scope = _org_uuid(org)
    if not dry_run:
        # The scheduled janitor owns the moving sweep and its shard lease; an
        # operator running a second mutator by hand is exactly how two sweepers
        # end up on one shard.
        console.print("[red]only --dry-run is supported; the janitor schedule owns the sweep[/red]")
        raise typer.Exit(EXIT_USAGE)

    async def main() -> int:
        async with operator() as (session, janitor):
            await _ensure_sweep_shard(session)
            plans: list[SweepPlan] = []
            for target in await _targets(session, scope):
                plan = await janitor.sweep(target.domain_id, org=target.org, dry_run=True)
                assert isinstance(plan, SweepPlan)
                plans.append(plan)
            return _print_gc(plans)

    _run(main)


async def _ensure_sweep_shard(session: AsyncSession, shard: int = 0) -> None:
    """Bring the shard row the plan stamps into being if nobody has yet.

    The migrations create ``file_sweep_shards`` empty and no write path inserts
    into it, so on a deployment that has never swept the first planner is what
    creates the row. ``gc --dry-run`` is very often that first planner — an
    operator asking "what would a sweep move here" before ever letting one run
    — and without this it would fail on a row it has no verb to create.

    ``ON CONFLICT DO NOTHING`` and committed on its own: a second operator, or
    the scheduled janitor, may be doing the same thing on the same shard, and
    neither the cursor nor ``sweep_started_at`` of a sweep already in flight may
    be disturbed by the one that lost the race.
    """
    await session.execute(
        text(
            "INSERT INTO file_sweep_shards (shard, cursor) VALUES (:shard, '{}'::jsonb) "
            "ON CONFLICT (shard) DO NOTHING"
        ),
        {"shard": shard},
    )
    await session.commit()


def _print_gc(plans: Sequence[SweepPlan]) -> int:
    table = Table(title="files gc --dry-run")
    for column in ("domain", "objects", "bytes", "live bytes", "breaker"):
        table.add_column(column)
    tripped = False
    for plan in plans:
        tripped = tripped or plan.breaker_tripped
        table.add_row(
            str(plan.domain_id),
            str(len(plan.candidates)),
            str(plan.moved_bytes),
            str(plan.live_bytes),
            "tripped" if plan.breaker_tripped else "ok",
        )
    console.print(table)
    for plan in plans:
        for candidate in plan.candidates:
            console.print(f"would move {candidate.key} ({candidate.size} bytes)")
    return EXIT_FINDINGS if tripped else EXIT_OK


@quarantine_app.command("list")
def quarantine_list(org: OrgOpt = None) -> None:
    """Everything a sweeper refused to act on, oldest first."""
    scope = _org_uuid(org)

    async def main() -> int:
        async with operator() as (session, _janitor):
            statement = (
                "SELECT id, org_team_id, kind, ref_id, reason, attempts FROM file_quarantine "
                "WHERE resolved_at IS NULL ORDER BY first_seen_at"
                if scope is None
                else "SELECT id, org_team_id, kind, ref_id, reason, attempts FROM file_quarantine "
                "WHERE resolved_at IS NULL AND org_team_id = CAST(:org AS uuid) "
                "ORDER BY first_seen_at"
            )
            rows = (await session.execute(text(statement), _org_params(scope))).all()
            table = Table(title="files quarantine")
            for column in ("id", "org", "kind", "ref", "reason", "attempts"):
                table.add_column(column)
            for row in rows:
                table.add_row(
                    str(row.id),
                    str(row.org_team_id),
                    row.kind,
                    row.ref_id,
                    row.reason,
                    str(row.attempts),
                )
            console.print(table)
            return EXIT_FINDINGS if rows else EXIT_OK

    _run(main)


@quarantine_app.command("retry")
def quarantine_retry(
    entry: Annotated[str, typer.Argument(help="Quarantine row id to re-open.")],
) -> None:
    """Count another attempt on one entry and clear its resolution.

    Retry does not re-run the sweeper itself: the next scheduled pass picks the
    entry up. What this verb owns is the record that somebody asked for it.
    """
    _run(lambda: _quarantine_update(entry, resolve=False))


@quarantine_app.command("discard")
def quarantine_discard(
    entry: Annotated[str, typer.Argument(help="Quarantine row id to resolve.")],
) -> None:
    """Mark one entry resolved. The referenced row and object are left alone."""
    _run(lambda: _quarantine_update(entry, resolve=True))


async def _quarantine_update(entry: str, *, resolve: bool) -> int:
    try:
        entry_id = str(uuid.UUID(entry))
    except ValueError:
        console.print(f"[red]not a uuid:[/red] {entry}")
        return EXIT_USAGE
    # ``RETURNING`` rather than a row count: the async result's count is not on
    # the typed surface, and the returned id is the same evidence.
    statement = (
        "UPDATE file_quarantine SET resolved_at = now() WHERE id = CAST(:id AS uuid) RETURNING id"
        if resolve
        else "UPDATE file_quarantine SET attempts = attempts + 1, resolved_at = NULL "
        "WHERE id = CAST(:id AS uuid) RETURNING id"
    )
    async with operator() as (session, _janitor):
        updated = (await session.execute(text(statement), {"id": entry_id})).first()
        await session.commit()
        if updated is None:
            console.print(f"[red]no quarantine entry:[/red] {entry}")
            return EXIT_USAGE
        console.print(f"{'discarded' if resolve else 'retried'} {entry_id}")
        return EXIT_OK


@admin_app.command("restore-drill")
def restore_drill(
    org: OrgOpt = None,
    apply: Annotated[
        bool, typer.Option("--apply", help="Actually bump the platform restore generation.")
    ] = False,
) -> None:
    """Rehearse the restore runbook's epoch bump.

    Without ``--apply`` this prints the plan and the arithmetic and touches
    nothing: the restore generation is the high half of every lease epoch, so a
    drill that bumped it for real would fence every live lease on the platform.
    """
    scope = _org_uuid(org)

    async def main() -> int:
        async with operator() as (session, _janitor):
            current = (
                await session.execute(
                    text("SELECT restore_generation FROM file_platform WHERE id = 1")
                )
            ).scalar_one_or_none()
            if current is None:
                console.print("[red]file_platform has no row; run the migrations[/red]")
                return EXIT_USAGE
            counted = (
                "SELECT count(*) FROM file_leases"
                if scope is None
                else "SELECT count(*) FROM file_leases WHERE org_team_id = CAST(:org AS uuid)"
            )
            leases = (await session.execute(text(counted), _org_params(scope))).scalar_one()
            table = Table(title="files restore-drill")
            table.add_column("field")
            table.add_column("value")
            table.add_row("restore generation", str(current))
            table.add_row("would become", str(int(current) + 1))
            table.add_row("leases fenced", str(leases))
            table.add_row("mode", "apply" if apply else "dry run")
            console.print(table)
            if not apply:
                console.print("dry run: file_platform untouched")
                return EXIT_OK
            await session.execute(
                text(
                    "UPDATE file_platform SET restore_generation = restore_generation + 1 "
                    "WHERE id = 1"
                )
            )
            await session.commit()
            console.print(f"restore generation is now {int(current) + 1}")
            return EXIT_OK

    _run(main)


@admin_app.command("inspect")
def inspect_command(
    ref: Annotated[str, typer.Argument(help="A node id, a version id or a store key.")],
) -> None:
    """Print the row, the store key, the object's head and its encryption metadata."""

    async def main() -> int:
        async with operator() as (session, janitor):
            row = await _resolve(session, ref)
            if row is None:
                console.print(f"[red]nothing matches:[/red] {ref}")
                return EXIT_USAGE
            absolute = f"{DOMAIN_PREFIX}{row.dedup_domain_id}/{row.store_key}"
            table = Table(title="files inspect")
            table.add_column("field")
            table.add_column("value")
            table.add_row("version", str(row.id))
            table.add_row("node", str(row.node_id))
            table.add_row("org", str(row.org_team_id))
            table.add_row("seq", str(row.seq))
            table.add_row("size", str(row.size_bytes))
            table.add_row("content hash", str(row.content_hash))
            table.add_row("store key", absolute)
            info = await janitor._store.head(absolute)
            if info is None:
                # A row whose object is gone is the finding, not an error: the
                # operator asked what is there and the answer is "nothing".
                table.add_row("object", "missing")
                console.print(table)
                return EXIT_FINDINGS
            table.add_row("object size", str(info.size))
            table.add_row("object etag", str(info.etag or ""))
            table.add_row(
                "encryption", json.dumps(_encryption_of(dict(row.metadata or {})), sort_keys=True)
            )
            console.print(table)
            return EXIT_OK

    _run(main)


async def _resolve(session: AsyncSession, ref: str) -> Any:
    """The version row a node id, a version id or a store key names.

    Three separate lookups rather than one clever ``OR``: a store key is not a
    uuid, so a single statement would have to cast it and fail on the way to
    the branch that would have matched.
    """
    select = (
        "SELECT v.id, v.node_id, v.org_team_id, v.seq, v.size_bytes, v.content_hash, "
        "v.store_key, v.metadata, d.dedup_domain_id FROM file_versions v "
        "JOIN file_nodes n ON n.id = v.node_id "
        "JOIN file_drives d ON d.id = n.drive_id WHERE "
    )
    try:
        as_uuid: str | None = str(uuid.UUID(ref))
    except ValueError:
        as_uuid = None
    if as_uuid is not None:
        for clause in ("v.id = CAST(:ref AS uuid)", "n.id = CAST(:ref AS uuid)"):
            row = (
                await session.execute(
                    text(f"{select}{clause} ORDER BY v.seq DESC"), {"ref": as_uuid}
                )
            ).first()
            if row is not None:
                return row
        return None
    return (await session.execute(text(f"{select}v.store_key = :ref"), {"ref": ref})).first()


def _encryption_of(metadata: dict[str, Any]) -> dict[str, Any]:
    """The encryption facts out of a version's metadata, and nothing else."""
    return {key: metadata[key] for key in ENCRYPTION_KEYS if key in metadata}


__all__ = [
    "ENCRYPTION_KEYS",
    "EXIT_FINDINGS",
    "EXIT_OK",
    "EXIT_USAGE",
    "Target",
    "admin_app",
    "build_store_factory",
    "operator",
]
