"""Deployment health runner — shared by the worker's 5-minute schedule and the
org-admin manual-run endpoint.

Lives in ``alkera_core`` because the worker may only import alkera_core. Each
check is an async callable that returns one or more :class:`CheckResult`s; the
runner fans them out with a per-check timeout and a total budget, so one hanging
probe can never sink the run. Detail strings are canned and name the env var to
fix — never a secret, never a raw exception string from an auth flow, and URLs
are scrubbed.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Literal, Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from uuid import UUID

import httpx
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest

from alkera_core.config import settings
from alkera_core.db.locking import advisory_key, advisory_xact_lock
from alkera_core.db.schema_head import EXPECTED_SCHEMA_HEAD
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.entitlements import byok_active, get_entitlements
from alkera_core.extensions import ExtensionPoint
from alkera_core.files.ids import DomainId
from alkera_core.files.store import NoSuchBucket, NotFound, ObjectStore, Throttled, absolute
from alkera_core.http import async_client
from alkera_core.llm_provider import Provider
from alkera_core.logging import get_logger
from alkera_core.model_providers import probe_provider_credentials, resolve_all
from alkera_core.models import DeploymentHealthCheck, DeploymentHealthRun, ModelProviderConfig
from alkera_core.observability.metrics import record_files_store_available
from alkera_core.temporal import connect_client

log = get_logger(__name__)

CheckStatus = Literal["ok", "warn", "fail", "skipped"]
Trigger = Literal["scheduled", "manual"]

PER_CHECK_TIMEOUT_S = 8.0
RUN_BUDGET_S = 15.0
# A scheduled run every 5 min; > ~2 missed ticks + runtime ⇒ the worker or its
# schedule is down.
SCHEDULED_STALE_AFTER = timedelta(minutes=12)
# The Temporal probe's budget for each of its two steps (connect, describe the
# namespace); well inside PER_CHECK_TIMEOUT_S so the probe names its own failure
# instead of the runner reporting a generic timeout.
TEMPORAL_PROBE_TIMEOUT_S = 3.0

# The object-store probe's budget. Deliberately short: readiness calls it on
# every scrape, and a store that has not answered in two seconds is one that
# cannot serve an upload either.
FILES_STORE_PROBE_TIMEOUT_S = 2.0

#: The key the probe HEADs. It is a well-formed absolute key under the all-zero
#: domain, which no org can ever be allocated, so the probe reads nothing real
#: and a 404 is a perfectly good answer — what is being proven is that the store
#: answered at all, not that any object exists.
FILES_STORE_PROBE_KEY = absolute(DomainId(UUID(int=0)), "health/probe")


@dataclass(frozen=True, slots=True)
class CheckResult:
    key: str
    label: str
    status: CheckStatus
    detail: str
    latency_ms: int = 0
    org_team_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class RunMeta:
    last_run_at: datetime
    last_trigger: Trigger
    last_duration_ms: int
    last_scheduled_at: datetime | None


@dataclass
class CheckContext:
    trigger: Trigger
    session_factory: async_sessionmaker[AsyncSession]
    http_client: httpx.AsyncClient
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    # The Temporal probe's connection seam: a coroutine factory returning a
    # ``temporalio.client.Client``-shaped object. None → the settings-driven
    # ``connect_client`` (the same address, TLS and identity rules as the worker).
    temporal_client_factory: Callable[[], Awaitable[Any]] | None = None
    # The Files object-store seam: a callable returning the bucket-wide admin
    # handle, which the process that owns a store passes in (the backend does).
    # None → the check reports "skipped": the worker holds no store, and
    # alkera_core never reaches into an app to find one.
    files_admin_store: Callable[[], ObjectStore] | None = None


Check = Callable[[CheckContext], Awaitable[list[CheckResult]]]


# Query-string parameters that carry a secret in a connection URL. Some managed
# stores accept ``?password=…`` and some pass a token the same way — those must be
# masked too, not just the userinfo password.
_SECRET_URL_PARAMS = frozenset({"password", "pwd", "token", "authtoken", "auth", "secret"})


def scrub_url(url: str) -> str:
    """A connection URL with any password masked — both the ``user:pass@host``
    userinfo form AND the ``?password=…`` query-string form. This value lands in the
    org-admin health report and is persisted, so it must never carry a live secret."""
    try:
        parts = urlparse(url)
    except ValueError:
        return "<unparseable>"
    # Reconstruct from parts and mask WITHIN each segment — a naive full-string
    # replace would corrupt the host/path/port if the secret value appeared there
    # too (e.g. password "dev" inside host "db-dev.internal").
    netloc = parts.netloc
    if parts.password:
        netloc = netloc.replace(f":{parts.password}@", ":***@", 1)
    query = parts.query
    if query:
        query = urlencode(
            [
                (key, "***" if (value and key.lower() in _SECRET_URL_PARAMS) else value)
                for key, value in parse_qsl(query, keep_blank_values=True)
            ],
            safe="*",  # keep the "***" mask literal rather than percent-encoding it
        )
    return urlunparse(parts._replace(netloc=netloc, query=query))


# --- the schedule-liveness self-check (pure; reused at read time) ----------------


def worker_beat_result(last_scheduled_at: datetime | None, now: datetime) -> CheckResult:
    if last_scheduled_at is None:
        return CheckResult(
            "worker_beat",
            "Background worker & scheduler",
            "warn",
            "no scheduled health run recorded yet — the worker may still be starting "
            "(expected every 5 minutes)",
        )
    age = now - last_scheduled_at
    if age <= SCHEDULED_STALE_AFTER:
        return CheckResult(
            "worker_beat", "Background worker & scheduler", "ok", "scheduled runs are current"
        )
    minutes = int(age.total_seconds() // 60)
    return CheckResult(
        "worker_beat",
        "Background worker & scheduler",
        "fail",
        f"the last scheduled run was {minutes}m ago — is the worker container running and "
        "are its schedules synced (python -m worker schedules list)?",
    )


# --- individual checks -----------------------------------------------------------


async def _check_postgres(ctx: CheckContext) -> list[CheckResult]:
    started = time.monotonic()
    try:
        async with ctx.session_factory() as db:
            await asyncio.wait_for(db.execute(text("SELECT 1")), timeout=2.0)
        return [_ok("postgres", "Postgres", "database reachable", started)]
    except Exception:
        return [
            _fail(
                "postgres",
                "Postgres",
                "database unreachable — check DATABASE_URL / network",
                started,
            )
        ]


async def _check_migrations(ctx: CheckContext) -> list[CheckResult]:
    started = time.monotonic()
    try:
        async with ctx.session_factory() as db:
            row = (await db.execute(text("SELECT version_num FROM alembic_version"))).first()
    except Exception:
        return [
            _fail(
                "migrations",
                "Database migrations",
                "no alembic_version table — the database was never migrated "
                "(run `alembic upgrade head`)",
                started,
            )
        ]
    current = row[0] if row else None
    if current == EXPECTED_SCHEMA_HEAD:
        return [_ok("migrations", "Database migrations", "schema is at the expected head", started)]
    return [
        _fail(
            "migrations",
            "Database migrations",
            f"database is at {current!r}, this build expects {EXPECTED_SCHEMA_HEAD!r} — "
            "run `alembic upgrade head`",
            started,
        )
    ]


async def _check_temporal(ctx: CheckContext) -> list[CheckResult]:
    """Connect to the Temporal frontend and have it describe the configured
    namespace — the handshake the worker performs at boot, so "ok" here means a
    worker could serve. Each step is bounded on its own, and the detail names the
    settings to check, never the API key or the transport's error text."""
    started = time.monotonic()
    connect = ctx.temporal_client_factory or partial(connect_client, component="deployment-health")
    try:
        client = await asyncio.wait_for(connect(), timeout=TEMPORAL_PROBE_TIMEOUT_S)
        await asyncio.wait_for(
            client.service_client.workflow_service.describe_namespace(
                DescribeNamespaceRequest(namespace=client.namespace)
            ),
            timeout=TEMPORAL_PROBE_TIMEOUT_S,
        )
    except Exception:
        return [
            _fail(
                "temporal",
                "Task orchestrator",
                f"Temporal unreachable at {settings.temporal_address} (namespace "
                f"{settings.temporal_namespace!r}) — check TEMPORAL_ADDRESS / "
                "TEMPORAL_NAMESPACE / TEMPORAL_API_KEY",
                started,
            )
        ]
    return [
        _ok(
            "temporal",
            "Task orchestrator",
            f"namespace {settings.temporal_namespace!r} reachable",
            started,
        )
    ]


async def _check_worker_beat(ctx: CheckContext) -> list[CheckResult]:
    # A scheduled run IS the proof the schedule delivered this run; a manual run
    # reads the last scheduled stamp so a dead worker still surfaces.
    if ctx.trigger == "scheduled":
        return [
            CheckResult(
                "worker_beat", "Background worker & scheduler", "ok", "scheduled run delivered"
            )
        ]
    if not settings.is_self_hosted:
        # The scheduled snapshot self-skips on SaaS (the worker task never
        # stamps the marker), so reading it here would warn forever; the fleet's
        # own alarms watch the workers there.
        return [
            _skipped(
                "worker_beat",
                "Background worker & scheduler",
                "the scheduled snapshot self-skips on SaaS; worker liveness is watched by the "
                "fleet's alarms",
            )
        ]
    async with ctx.session_factory() as db:
        run = await db.get(DeploymentHealthRun, 1)
    return [worker_beat_result(run.last_scheduled_at if run else None, ctx.now())]


async def _check_model_gateway(ctx: CheckContext) -> list[CheckResult]:
    """Whether the gateway's own dependencies answer right now.

    Asked with ``?strict=1`` and read from the body: the plain probe answers a
    load balancer, and a gateway inside its grace window answers it 200 with
    ``status: "degraded"``. A check that trusted the status code alone called
    that gateway ready. The body is read as well as the strict status, so a
    gateway too old to know ``strict`` is still judged by what it says.
    """
    started = time.monotonic()
    url = settings.gateway_base_url.rstrip("/") + "/health/ready"
    try:
        resp = await ctx.http_client.get(url, params={"strict": "1"}, timeout=5.0)
    except httpx.HTTPError:
        return [
            _fail(
                "model_gateway",
                "Model gateway",
                "gateway unreachable — check GATEWAY_BASE_URL and the gateway container",
                started,
            )
        ]
    reported = _reported_status(resp)
    if resp.status_code == 200 and reported == "ok":
        return [_ok("model_gateway", "Model gateway", "gateway ready", started)]
    if resp.status_code == 200:
        detail = f"gateway answered 200 but reports {reported or 'no status'}"
    else:
        detail = f"gateway responded {resp.status_code}"
    return [
        _warn(
            "model_gateway",
            "Model gateway",
            f"{detail} (its own dependencies may be degraded)",
            started,
        )
    ]


def _reported_status(resp: httpx.Response) -> str | None:
    """The ``status`` a readiness body names, or ``None`` when it names none."""
    try:
        body = resp.json()
    except ValueError:
        return None
    reported = body.get("status") if isinstance(body, dict) else None
    return reported if isinstance(reported, str) else None


_PROVIDER_LABEL = {
    Provider.ANTHROPIC: "Anthropic API key",
    Provider.OPENAI: "OpenAI API key",
    Provider.BEDROCK: "Bedrock credentials",
}


async def _check_providers(ctx: CheckContext) -> list[CheckResult]:
    if settings.gateway_proxy_mode:
        return [
            _skipped(
                f"provider_{p.value}",
                _PROVIDER_LABEL[p],
                "Alkera holds provider keys in proxy mode",
            )
            for p in _PROVIDER_LABEL
        ]
    results: list[CheckResult] = []
    async with ctx.session_factory() as db:
        org_ids = set((await db.execute(select(ModelProviderConfig.org_team_id))).scalars().all())
        for org_id in org_ids:
            creds = await resolve_all(db, org_id)
            for provider, cred in creds.items():
                started = time.monotonic()
                probe = await probe_provider_credentials(
                    cred, http_client=ctx.http_client, timeout_s=6.0
                )
                results.append(
                    CheckResult(
                        f"provider_{provider.value}",
                        _PROVIDER_LABEL[provider],
                        _provider_status(probe.classification),
                        probe.detail,
                        _ms(started),
                        org_team_id=org_id,
                    )
                )
    if not results:
        # No org rows configured — note the instance env fallback state instead.
        results.extend(_env_fallback_rows())
    return results


def _env_fallback_rows() -> list[CheckResult]:
    """The provider rows when no organization holds keys of its own: what THIS
    process's environment carries. On SaaS the keys live on the model gateway,
    not here, so an empty env is the expected shape there, not a warning."""
    rows: list[CheckResult] = []
    for provider in _PROVIDER_LABEL:
        key, label = f"provider_{provider.value}", _PROVIDER_LABEL[provider]
        if _env_provider_configured(provider):
            rows.append(
                CheckResult(key, label, "ok", "configured via environment on this instance")
            )
        elif not settings.is_self_hosted:
            rows.append(
                _skipped(
                    key,
                    label,
                    "held by the model gateway on this deployment; an organization's own keys "
                    "under Organization → Deployment → Model providers take precedence",
                )
            )
        else:
            rows.append(
                CheckResult(
                    key,
                    label,
                    "warn",
                    "no credentials configured — add keys under "
                    "Organization → Deployment → Model providers",
                )
            )
    return rows


@runtime_checkable
class _BucketProbe(Protocol):
    """A driver that can be asked whether its container exists at all.

    Not part of :class:`ObjectStore`: a store with no container to miss (the
    filesystem driver) has nothing to answer, so the probe asks only the drivers
    that can and leaves the rest on the key HEAD alone.
    """

    async def head_bucket(self) -> None: ...


async def probe_files_store(store: ObjectStore) -> bool:
    """HEAD the probe key and report whether the store *answered*, driving the
    ``files_store_available`` gauge either way.

    Shared by this runner and the backend's ``/health/ready`` so both agree on
    the budget, the key and the metric.

    Reachability is what is being measured, not the existence of an object. The
    probe key is never written, so on any fresh bucket the honest answer is a
    404 — and a store that says "no such key" has just proven it is reachable,
    its credentials are accepted and its bucket is there. Treating that as an
    outage is how an instance behind this probe never becomes healthy at all.

    Unreachable is the narrow set that means bytes cannot be served: a transport
    error or unmapped 5xx (:class:`~alkera_core.files.store.errors.Unavailable`),
    a timeout, an auth refusal (:class:`~alkera_core.files.store.errors.AccessDenied`
    or :class:`~alkera_core.files.store.errors.ExpiredCredentials`) and a bucket
    that does not exist (:class:`~alkera_core.files.store.errors.NoSuchBucket`,
    which the driver keeps distinct from a missing key for exactly this call).
    Anything else unexpected fails closed.
    """
    try:
        if isinstance(store, _BucketProbe):
            # The key HEAD cannot answer "is the bucket even there". S3 replies
            # to a HEAD with no body, so the 404 it returns under a bucket that
            # does not exist carries no error code and arrives here as a plain
            # absence -- which the paragraph above reads as proof of health, so
            # a mistyped bucket name would answer green forever. A bucket-level
            # HEAD has no such ambiguity; existence is asked there and the key
            # HEAD is left to do what it is good at, proving reachability.
            await asyncio.wait_for(store.head_bucket(), FILES_STORE_PROBE_TIMEOUT_S)
        await asyncio.wait_for(store.head(FILES_STORE_PROBE_KEY), FILES_STORE_PROBE_TIMEOUT_S)
    except NoSuchBucket:
        available = False
    except NotFound:
        available = True
    except Throttled:
        # The store answered, and asked us to come back later: it is up, and a
        # throttle on a HEAD is no reason to take the instance out of rotation.
        available = True
    except Exception:
        available = False
    else:
        available = True
    record_files_store_available(available)
    return available


async def _check_files_store(ctx: CheckContext) -> list[CheckResult]:
    key, label = "files_store", "Files object store"
    if not settings.files_enabled:
        # Files off is a deployment choice, not a fault: report it as skipped so
        # the run stays green, and leave the gauge alone (there is nothing to
        # be available or unavailable).
        return [_skipped(key, label, "Files is disabled (FILES_ENABLED=false)")]
    if ctx.files_admin_store is None:
        return [_skipped(key, label, "this process holds no object-store handle")]
    store = ctx.files_admin_store()
    started = time.monotonic()
    if await probe_files_store(store):
        return [_ok(key, label, "object store reachable", started)]
    return [
        _fail(
            key,
            label,
            f"the object store did not answer within {FILES_STORE_PROBE_TIMEOUT_S:g}s, refused "
            "the credential, or has no such bucket — check FILES_STORE_PROVIDER / "
            "FILES_STORE_ENDPOINT / FILES_STORE_BUCKET and its credentials",
            started,
        )
    ]


async def _check_smtp(ctx: CheckContext) -> list[CheckResult]:
    if not settings.email_enabled:
        return [
            _skipped(
                "smtp", "Email (SMTP)", "outbound email disabled by design (EMAIL_ENABLED=false)"
            )
        ]
    started = time.monotonic()
    import aiosmtplib

    try:
        client = aiosmtplib.SMTP(
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            timeout=min(settings.smtp_timeout_seconds, 6),
            use_tls=False,
            start_tls=False,
        )
        await client.connect()
        await client.ehlo()
        if settings.smtp_use_tls:
            await client.starttls()
        await client.quit()
        return [_ok("smtp", "Email (SMTP)", "relay reachable", started)]
    except Exception:
        return [
            _fail(
                "smtp",
                "Email (SMTP)",
                f"cannot reach the SMTP relay {settings.smtp_host}:{settings.smtp_port} — "
                "check SMTP_HOST / SMTP_PORT / firewall",
                started,
            )
        ]


async def _check_entitlement(ctx: CheckContext) -> list[CheckResult]:
    started = time.monotonic()
    ent = get_entitlements()
    state = ent.state(ctx.now())
    async with ctx.session_factory() as db:
        has_byok_rows = (
            await db.execute(select(ModelProviderConfig.id).limit(1))
        ).first() is not None
    if state == "valid":
        return [_ok("entitlement", "License / entitlement", "entitlement valid", started)]
    if state == "grace":
        until = ent.grace_until()
        return [
            CheckResult(
                "entitlement",
                "License / entitlement",
                "warn",
                f"entitlement expired — in grace until "
                f"{until.isoformat() if until else 'soon'}; renew with Alkera",
            )
        ]
    if state in ("expired", "invalid"):
        return [
            CheckResult("entitlement", "License / entitlement", "fail", f"entitlement is {state}")
        ]
    # absent
    if has_byok_rows:
        return [
            CheckResult(
                "entitlement",
                "License / entitlement",
                "fail",
                "BYOK credentials are configured but no valid entitlement is present",
            )
        ]
    return [_skipped("entitlement", "License / entitlement", "no entitlement configured")]


async def _check_entitlement_consistency(ctx: CheckContext) -> list[CheckResult]:
    """The gateway meters + bills BYOK, while THIS process (backend/worker) grants
    cycle credits and reports ``billing_mode`` to the SPA — ``byok_active()`` must
    agree across all of them, which needs ``ALKERA_ENTITLEMENTS`` set identically on
    every process. Read the gateway's self-reported BYOK state (from /health/ready)
    and compare with this process's view; a split means half-active BYOK — phantom
    Alkera-side credits on one side while the gateway bills at provider cost."""
    key, label = "entitlement_consistency", "BYOK entitlement consistency"
    if settings.gateway_proxy_mode or not settings.is_self_hosted:
        return [_skipped(key, label, "not a BYOK-capable deployment (proxy upstream or SaaS)")]
    started = time.monotonic()
    url = settings.gateway_base_url.rstrip("/") + "/health/ready"
    try:
        resp = await ctx.http_client.get(url, timeout=5.0)
        gateway_byok = resp.json().get("byok")
    except (httpx.HTTPError, ValueError, AttributeError):
        return [_skipped(key, label, "gateway state unavailable — see the Model gateway check")]
    if not isinstance(gateway_byok, bool):
        return [_skipped(key, label, "gateway does not report BYOK state (older build)")]
    if gateway_byok == byok_active():
        return [_ok(key, label, "the gateway and backend/worker agree on BYOK", started)]
    return [
        CheckResult(
            key,
            label,
            "fail",
            f"the gateway (byok={gateway_byok}) and this process (byok={byok_active()}) disagree "
            "— set ALKERA_ENTITLEMENTS identically on the gateway, backend, and worker",
        )
    ]


async def _check_secret_box(ctx: CheckContext) -> list[CheckResult]:
    started = time.monotonic()
    from alkera_core.auth.secret_box import decrypt_secret, encrypt_secret

    try:
        assert decrypt_secret(encrypt_secret("healthcheck")) == "healthcheck"
        return [_ok("secret_box", "Secrets-at-rest key", "encrypt/decrypt round-trips", started)]
    except Exception:
        return [
            _fail(
                "secret_box",
                "Secrets-at-rest key",
                "secret-box encrypt/decrypt failed — check "
                "SECRET_BOX_KEY / SECRET_BOX_KEYS_PREVIOUS",
                started,
            )
        ]


CHECK_REGISTRY: tuple[Check, ...] = (
    _check_postgres,
    _check_migrations,
    _check_temporal,
    _check_worker_beat,
    _check_model_gateway,
    _check_providers,
    _check_smtp,
    _check_files_store,
    _check_entitlement,
    _check_entitlement_consistency,
    _check_secret_box,
)


#: Checks a distribution adds to the open ones, registered during composition
#: (billing's model catalog, for one). They run after the open checks.
DEPLOYMENT_CHECKS: ExtensionPoint[Check] = ExtensionPoint("deployment_checks")


def registered_checks(point: ExtensionPoint[Check] = DEPLOYMENT_CHECKS) -> tuple[Check, ...]:
    """The open checks, then every one registered on ``point``. Freezes the point."""
    return (*CHECK_REGISTRY, *point.items())


# --- runner ----------------------------------------------------------------------


async def run_all_checks(
    ctx: CheckContext, checks: Sequence[Check] | None = None
) -> list[CheckResult]:
    registry = checks if checks is not None else registered_checks()

    async def _guarded(check: Check) -> list[CheckResult]:
        try:
            return await asyncio.wait_for(check(ctx), PER_CHECK_TIMEOUT_S)
        except TimeoutError:
            return [CheckResult(_name(check), _name(check), "fail", "the check timed out")]
        except Exception as exc:  # a check must never sink the run
            return [
                CheckResult(
                    _name(check), _name(check), "fail", f"the check errored ({type(exc).__name__})"
                )
            ]

    try:
        grouped = await asyncio.wait_for(
            asyncio.gather(*(_guarded(c) for c in registry)), RUN_BUDGET_S
        )
    except TimeoutError:
        return [CheckResult("run", "Health run", "fail", "the health run exceeded its time budget")]
    return [result for group in grouped for result in group]


async def persist_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    results: list[CheckResult],
    *,
    trigger: Trigger,
    started_at: datetime,
    duration_ms: int,
) -> None:
    async with session_factory() as db:
        # Serialize a manual/scheduled collision; last full snapshot wins.
        await advisory_xact_lock(db, advisory_key("deployment-health"))
        await db.execute(delete(DeploymentHealthCheck))
        db.add_all(
            [
                DeploymentHealthCheck(
                    check_key=r.key,
                    label=r.label,
                    org_team_id=r.org_team_id,
                    status=r.status,
                    detail=r.detail,
                    latency_ms=r.latency_ms,
                    ran_at=started_at,
                    trigger=trigger,
                )
                for r in results
            ]
        )
        set_: dict[str, Any] = {
            "last_run_at": started_at,
            "last_trigger": trigger,
            "last_duration_ms": duration_ms,
        }
        if trigger == "scheduled":
            set_["last_scheduled_at"] = started_at
        await db.execute(
            pg_insert(DeploymentHealthRun)
            .values(
                id=1,
                last_scheduled_at=started_at if trigger == "scheduled" else None,
                **{k: v for k, v in set_.items() if k != "last_scheduled_at"},
            )
            .on_conflict_do_update(index_elements=[DeploymentHealthRun.id], set_=set_)
        )
        await db.commit()


async def run_and_persist(
    *,
    trigger: Trigger,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
    files_admin_store: Callable[[], ObjectStore] | None = None,
) -> list[CheckResult]:
    """Run every check and full-replace the snapshot. ``files_admin_store`` is
    the caller's object-store handle; without one the Files check is skipped."""
    started_at = datetime.now(UTC)
    started = time.monotonic()
    client = async_client(timeout=8.0)
    try:
        ctx = CheckContext(
            trigger=trigger,
            session_factory=session_factory,
            http_client=client,
            files_admin_store=files_admin_store,
        )
        results = await run_all_checks(ctx)
    finally:
        await client.aclose()
    duration_ms = int((time.monotonic() - started) * 1000)
    await persist_snapshot(
        session_factory, results, trigger=trigger, started_at=started_at, duration_ms=duration_ms
    )
    return results


async def load_snapshot(
    db: AsyncSession, *, org_team_id: UUID
) -> tuple[list[CheckResult], RunMeta | None]:
    rows = (
        (
            await db.execute(
                select(DeploymentHealthCheck).where(
                    (DeploymentHealthCheck.org_team_id.is_(None))
                    | (DeploymentHealthCheck.org_team_id == org_team_id)
                )
            )
        )
        .scalars()
        .all()
    )
    results = [
        CheckResult(r.check_key, r.label, r.status, r.detail, r.latency_ms, r.org_team_id)  # type: ignore[arg-type]
        for r in rows
    ]
    run = await db.get(DeploymentHealthRun, 1)
    meta = (
        RunMeta(run.last_run_at, run.last_trigger, run.last_duration_ms, run.last_scheduled_at)  # type: ignore[arg-type]
        if run is not None
        else None
    )
    return results, meta


# --- small helpers ---------------------------------------------------------------


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _ok(key: str, label: str, detail: str, started: float) -> CheckResult:
    return CheckResult(key, label, "ok", detail, _ms(started))


def _warn(key: str, label: str, detail: str, started: float) -> CheckResult:
    return CheckResult(key, label, "warn", detail, _ms(started))


def _fail(key: str, label: str, detail: str, started: float) -> CheckResult:
    return CheckResult(key, label, "fail", detail, _ms(started))


def _skipped(key: str, label: str, detail: str) -> CheckResult:
    return CheckResult(key, label, "skipped", detail)


def _name(check: Check) -> str:
    return getattr(check, "__name__", "check").removeprefix("_check_")


def _provider_status(classification: str) -> CheckStatus:
    if classification == "ok":
        return "ok"
    if classification == "invalid_key":
        return "fail"
    return "warn"  # permission / network / unavailable


def _env_provider_configured(provider: Provider) -> bool:
    if provider is Provider.ANTHROPIC:
        return bool(settings.anthropic_api_key)
    if provider is Provider.OPENAI:
        return bool(settings.openai_api_key)
    return bool(
        settings.aws_bearer_token_bedrock
        or settings.aws_access_key_id
        or settings.gateway_assume_bedrock_iam
    )


__all__ = [
    "CHECK_REGISTRY",
    "FILES_STORE_PROBE_KEY",
    "FILES_STORE_PROBE_TIMEOUT_S",
    "SCHEDULED_STALE_AFTER",
    "TEMPORAL_PROBE_TIMEOUT_S",
    "CheckContext",
    "CheckResult",
    "CheckStatus",
    "RunMeta",
    "Trigger",
    "load_snapshot",
    "persist_snapshot",
    "probe_files_store",
    "run_all_checks",
    "run_and_persist",
    "scrub_url",
    "worker_beat_result",
]
