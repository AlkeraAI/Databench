"""Centralized settings — single source of truth for env-driven config.

All other apps (worker, CLI) that need shared settings should import from here.
"""

from __future__ import annotations

import ipaddress
import math
import os
import re
import unicodedata
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from alkera_core import generated_secrets
from alkera_core.compute.liveness import (
    CHAT_IDLE_MINUTES,
    CHAT_IDLE_PRODUCTION_FLOOR_MINUTES,
    CHAT_MEMORY_PRESSURE_PERCENT,
    DRAIN_CEILING_SECONDS,
    HEARTBEAT_INTERVAL_SECONDS,
    READY_WINDOW_SECONDS,
)
from alkera_core.env_files import ENV_FILE_NAMES, env_files
from alkera_core.gateway import (
    GATEWAY_CLIENT_SILENCE_SECONDS,
    GATEWAY_KEEPALIVES_PER_CLIENT_SILENCE,
)

AppEnv = Literal["local", "staging", "production"]
# The argon2id work factor the password hasher runs at. The cost each name maps
# to lives with the hasher (`backend.auth.password`); only the choice is config.
PasswordHashProfile = Literal["production", "fast"]

# File-mounted secrets (K8s/Docker `secrets:` → `/run/secrets/<KEY>`). pydantic-settings
# warns if the dir is set but absent, so only activate it when it actually exists — dev
# (no /run/secrets) is then untouched. Override the location with ALKERA_SECRETS_DIR.
_SECRETS_DIR = os.environ.get("ALKERA_SECRETS_DIR", "/run/secrets")
_ACTIVE_SECRETS_DIR = _SECRETS_DIR if Path(_SECRETS_DIR).is_dir() else None


class MissingServerSecretError(RuntimeError):
    """A server secret is unset and APP_ENV was not set to ``local`` on purpose,
    so the published dev fallback may not stand in for it."""


# Used by the auth layer when AUTH_JWT_SECRET is not set in `local` mode.
# Outside `local`, missing AUTH_JWT_SECRET raises at startup (see validator).
_LOCAL_DEV_JWT_FALLBACK = "alkera-local-dev-secret-do-not-use-in-prod"

# Used to keyed-hash (HMAC-SHA256) single-use lookup tokens — password-reset,
# email-verification, and invitation tokens — before they're persisted, so a DB
# read can't replay the emailed link. Falls back to this dev value in `local`;
# outside `local`, a missing TOKEN_HASH_PEPPER raises at startup (see validator).
_LOCAL_DEV_TOKEN_HASH_PEPPER = "alkera-local-dev-token-pepper-do-not-use-in-prod"  # noqa: S105

# Signs the short-lived content tokens the user-content domain checks. Falls back
# to this dev value in `local`; a real deployment sets FILES_CONTENT_SIGNING_KEY.
_LOCAL_DEV_FILES_CONTENT_SIGNING_KEY = "alkera-local-dev-files-content-key-do-not-use-in-prod"

# The committed credentials of the local SeaweedFS container
# (deploy/docker/seaweedfs-s3.json), which ops/scripts/workspace-env.sh writes into
# every worktree's .env.workspace so a fresh clone gets a working store with no
# hand-editing. They are public, so production must never boot with them — the
# refusal below is what keeps an inherited local env from reaching a real bucket.
_LOCAL_DEV_FILES_STORE_ACCESS_KEY = "alkera"
_LOCAL_DEV_FILES_STORE_SECRET_KEY = "alkera-dev-secret"  # noqa: S105

# What each store driver can do natively. A capability the driver lacks has a
# portable fallback in the core (presigned URLs -> `proxied` transfer, scoped
# credentials -> the in-process prefix guard), so this table decides which
# SETTINGS COMBINATIONS are coherent at boot, not which features exist. The
# driver's own record is the runtime truth; this is the boot-time hint.
FILES_STORE_CAPABILITIES: dict[str, frozenset[str]] = {
    "filesystem": frozenset({"conditional_write"}),
    "s3_compatible": frozenset({"conditional_write", "presigned_urls", "range_signed_urls"}),
    "aws": frozenset(
        {
            "conditional_write",
            "presigned_urls",
            "range_signed_urls",
            "scoped_credentials",
            "kms",
            "object_lock",
        }
    ),
}

# The persisted `file_stores.driver` for each configured provider, and the one
# place the two vocabularies meet. The MODEL's catalogue
# (`alkera_core.models.files.stores.STORE_DRIVERS`) is the stored spelling: a
# row records how the bytes are addressed, not which SDK vends the credentials,
# so both S3 providers persist as `s3`, and the settings keep three values only
# because the factory builds a different client for each. A provider with no
# row here is refused at boot rather than written to a column whose CHECK
# constraint would reject it mid-request.
FILES_STORE_DRIVERS: dict[str, str] = {
    "filesystem": "filesystem",
    "s3_compatible": "s3",
    "aws": "s3",
}

# Directories a production store root must never live under: they are wiped on
# reboot (and world-writable), so user bytes placed there are lost, not stored.
# S108 reads these literals as "code that writes to /tmp"; they are the denylist
# the validator refuses such a root by, which is the inverse.
_EPHEMERAL_ROOTS = (
    Path("/tmp"),  # noqa: S108
    Path("/var/tmp"),  # noqa: S108
    Path("/private/tmp"),
    Path("/private/var/tmp"),
)


def _host_of(url: str | None) -> str | None:
    """Lowercase hostname of a URL, port stripped. None when there is no host.

    Cookies are scoped by HOSTNAME and ignore the port, so every "is this the
    same origin as the app?" question in the Files validator is asked here.
    """
    if not url or not url.strip():
        return None
    raw = url.strip()
    parts = urlsplit(raw if "//" in raw else f"//{raw}")
    return parts.hostname or None


def _is_loopback_host(host: str) -> bool:
    """Whether ``host`` names this machine: ``localhost``, a ``*.localhost`` name,
    a loopback address or the unspecified address."""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def _frontend_base_url_errors(url: str) -> list[str]:
    """Production refusals for ``FRONTEND_BASE_URL``.

    The value is the base of every emailed link (invitation, password reset,
    email verification), the device grant's ``verification_uri``, the OAuth and
    SSO redirect target and one of the origins the CSRF guard trusts for cookie
    writes. Left at the local default, or pointed anywhere but a public https
    portal, every one of those silently breaks or trusts the wrong origin.
    """
    label = "FRONTEND_BASE_URL"
    if url != url.strip() or any(ch.isspace() or unicodedata.category(ch) == "Cc" for ch in url):
        return [f"{label} must be a bare https URL with no whitespace or control characters"]
    try:
        parts = urlsplit(url)
        host = parts.hostname
        # `.port` is where a malformed or out-of-range port surfaces (`:abc`,
        # `:99999`); `urlsplit` itself accepts them. A portal URL no browser can
        # open would still be stored, and the CSRF guard would then trust an
        # origin nothing ever sends, refusing every cookie write.
        port = parts.port
    except ValueError:
        return [f"{label} must be a valid https URL (e.g. https://app.example.com)"]
    if port == 0:
        return [f"{label} must be a valid https URL (e.g. https://app.example.com)"]
    errors: list[str] = []
    if parts.scheme != "https":
        errors.append(
            f"{label} must use https in production (it is the base of every emailed link "
            "and a CSRF-trusted origin)"
        )
    if not host:
        errors.append(f"{label} must name the public portal host (e.g. https://app.example.com)")
    elif _is_loopback_host(host):
        errors.append(f"{label} must be the public portal URL, not a localhost or loopback address")
    if parts.username is not None or parts.password is not None:
        errors.append(f"{label} must not carry credentials")
    if parts.query or parts.fragment or url.endswith(("?", "#")):
        errors.append(f"{label} must not carry a query string or fragment")
    return errors


def _under_ephemeral_root(root: Path) -> bool:
    """Whether `root` sits in (or is) an OS scratch directory."""
    normalized = Path(os.path.normpath(str(root)))
    return any(normalized == base or base in normalized.parents for base in _EPHEMERAL_ROOTS)


#: Connection ceiling of an outbound httpx client built by `alkera_core.http.
#: async_client` — it passes exactly this as its `limits=` (see
#: `alkera_core.http.UPSTREAM_POOL_LIMITS`), so the number is ours rather than
#: whatever httpx happens to default to. This is the REAL cap on concurrent
#: upstream provider calls per gateway process, so the backpressure gate below
#: has to shed under it. It is deliberately httpx's historical default: the same
#: factory serves every outbound caller (OAuth/OIDC, JWKS, captcha, health), so
#: changing it re-tunes all of them, not just the gateway.
#: Where the web proxy serves the model gateway on the app's origin
#: (``apps/web/nginx.gateway.template``), and so where a node is told to find
#: it when ``ALKERA_NODE_GATEWAY_URL`` is unset.
NODE_GATEWAY_PATH = "/gateway"

UPSTREAM_POOL_MAX_CONNECTIONS = 100

#: The most redirects `EGRESS_MAX_REDIRECTS` may ask a guarded fetch to follow.
#: Every hop is resolved, vetted and pinned, so a long chain is a real cost the
#: page being fetched controls; browsers stop around twenty.
EGRESS_MAX_REDIRECTS_CEILING = 20

#: The longest `EGRESS_DNS_TIMEOUT_SECONDS` may be. The deadline exists so one
#: unresponsive authoritative server cannot hold a request thread for the OS
#: resolver's own bound (minutes); a ceiling above that would defeat it.
EGRESS_DNS_TIMEOUT_CEILING_SECONDS = 60.0

#: Width of the `request_id` column a client's idempotency key is persisted in
#: (`ProxyRequest.request_id`, `UsageRecord.request_id`). The gateway's accepted
#: key length is validated against it, so an accepted key always fits.
_REQUEST_ID_COLUMN_LENGTH = 255

#: Floor the production validator holds the gateway's upstream silence bounds to.
#: Well under any legitimate reasoning pause, well over a transient network stall.
_MIN_UPSTREAM_SILENCE_SECONDS = 60.0

#: The widest health-check timeout a load balancer in front of this service
#: allows (an ALB caps its own at 120s but the target group's must stay under the
#: 30s check interval; ingress-nginx and the compose healthchecks are the same
#: order). `/health/ready`'s internal budget must stay under it, or the probe
#: gives up before the endpoint can answer and a slow database reads as a dead
#: host in every rotation at once.
_MAX_HEALTH_CHECK_TIMEOUT_SECONDS = 30.0


class OrgUsageMetric(StrEnum):
    """Metric sections `GET /api/v1/org/usage` can disclose; each member names
    one optional field of `OrgUsageResponse`. ORG_USAGE_METRICS selects a subset."""

    TOTALS = "totals"
    BY_METER = "by_meter"
    DAILY = "daily"
    BY_MODEL = "by_model"
    BY_MEMBER = "by_member"
    BY_TEAM = "by_team"
    TOKENS = "tokens"
    BY_CREDIT_CLASS = "by_credit_class"
    BY_PROVIDER = "by_provider"
    RELIABILITY = "reliability"
    RECENT = "recent"


#: The full disclosure set — what ORG_USAGE_METRICS="all" resolves to.
ORG_USAGE_METRIC_KEYS: frozenset[str] = frozenset(m.value for m in OrgUsageMetric)


# The task queues a worker may serve. The enum lives in
# `alkera_core.temporal.contract`; that package's client half imports this
# module, so the names are repeated here (a test pins the two spellings equal).
TASK_QUEUE_NAMES: tuple[str, ...] = ("money", "email", "sync", "default")

#: The longest ABSOLUTE browser-session lifetime a production deployment may
#: configure. Activity slides the idle windows; nothing slides this one, so it
#: is the bound that decides how long a compromised session can be used at all.
AUTH_ABSOLUTE_LIFETIME_MAX_SECONDS = 60 * 60 * 24 * 30

#: The widest concurrent-refresh grace a production deployment may configure.
#: Inside the grace a used refresh token is handed its existing successor
#: instead of ending the family, so the grace is how long a copied token can
#: ride a legitimate rotation undetected.
AUTH_REFRESH_REUSE_GRACE_MAX_SECONDS = 60


#: Bytes ONE in-flight proxied upload part keeps resident in a backend process,
#: whatever its Content-Length. The part PUT streams: a wire chunk is handed to
#: the store driver and released before the next one is read, so what a part
#: costs is the driver's own staging buffer, not the part. The S3 driver is the
#: expensive case: it re-packs the stream into ``MIN_PART_BYTES`` (5 MiB)
#: multipart buffers, and at the moment one is cut four 5 MiB regions are alive
#: at once -- the bytearray it accumulates in (with its growth slack), the
#: slice taken from it, the ``bytes`` copy of that slice handed to the SDK, and
#: the remainder -- plus one wire chunk (64 KiB under uvicorn's flow control):
#: 4 x 5 MiB + 64 KiB = 20.06 MiB by arithmetic, 20.3 MiB measured. The
#: filesystem driver writes each wire chunk straight through and stays under
#: 1 MiB. 24 MiB is that S3 peak with headroom for the SDK's request framing;
#: the measurement lives in ``apps/backend/tests/files/test_files_part_residency.py``
#: and fails the day a driver starts holding more (or, once the driver cuts
#: its parts through a memoryview, the day it holds half as much and this
#: number can drop). A part that stalls holds the same 24 MiB and nothing
#: else; a part that was announced and never sent holds nothing, because the
#: slot is taken when its first byte arrives.
FILES_UPLOAD_PART_RESIDENT_BYTES = 24 * (1 << 20)

#: How many parts the default resident budget admits at once, per process.
#: Fifty is the product requirement (many people uploading at the same time,
#: three or four files each) and the memory arithmetic is
#: ``50 x FILES_UPLOAD_PART_RESIDENT_BYTES = 1200 MiB``: 29 % of the 4 GiB SaaS
#: task (one uvicorn process) next to a
#: ~300 MiB idle process, leaving 2.5 GiB for everything else the task serves.
#: The Helm chart's 512 Mi backend limit cannot carry it and must set
#: ``FILES_UPLOAD_RESIDENT_BUDGET_BYTES`` to what its pod can (eight parts is
#: 192 MiB) or raise its limit.
FILES_UPLOAD_DEFAULT_CONCURRENT_PARTS = 50

#: The per-connection websocket frame ceiling, in bytes -- what every launch
#: line passes as uvicorn's ``--ws-max-size`` and therefore what one socket may
#: make the process buffer before the application sees a byte.
#:
#: It is NOT a setting, because the launch lines are static strings a running
#: process cannot re-read, and a knob that silently disagrees with the transport
#: is worse than a constant. It restates ``alkera_core.events.outbox``'s
#: ``MAX_PAYLOAD_BYTES + _FRAME_ENVELOPE_BYTES`` (that module imports settings,
#: so the dependency cannot run the other way); the two are pinned equal by
#: ``packages/api-core/tests/test_settings.py``, which fails the day either
#: moves on its own. The operator-facing half of the arithmetic is
#: ``REALTIME_WS_FRAME_BUDGET_BYTES``, which this multiplies against
#: ``REALTIME_WS_MAX_CONNECTIONS`` at boot.
REALTIME_WS_MAX_FRAME_BYTES = 2 * (1 << 20) + 64 * 1024


#: The server secrets ``GENERATED_SECRETS_DIR`` may generate when unset.
_GENERATED_SECRETS: Final = ("auth_jwt_secret", "token_hash_pepper", "files_content_signing_key")


def _set(value: object) -> bool:
    """Whether a raw settings input holds a value (a blank string is unset)."""
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    return value is not None and str(value).strip() != ""


class SettingsSection(BaseSettings):
    """A set of settings one owner declares, read from the same sources.

    The open platform's settings are :class:`Settings`. A distribution that adds
    features declares their settings in a section of its own, beside the code
    that reads them, so the open class carries only the open platform's
    settings. Every section reads the same environment, dotenv files and
    mounted secrets, so an operator sets every name the same way.
    """

    model_config = SettingsConfigDict(
        # `.env` carries the committed-shape config; `.env.workspace` (gitignored,
        # generated by ops/scripts/workspace-env.sh) overlays per-worktree infra
        # (ports + DB/Temporal/gateway URLs) so a plain `uv run …` from the repo root
        # hits this workspace's stack; `.env.local` (gitignored) overlays real
        # local-dev secrets and WINS on conflict — pydantic-settings applies later
        # files last. In production none of these exist; values come from the
        # environment. Which directory they are read from is
        # alkera_core.env_files' rule, applied when an instance is built (see
        # settings_customise_sources); `_env_file=` still overrides it.
        env_file=ENV_FILE_NAMES,
        env_file_encoding="utf-8",
        # File-mounted secrets (`/run/secrets/<KEY>`, K8s/Docker). Env vars still take
        # precedence (standard pydantic-settings order); None in dev = unchanged.
        secrets_dir=_ACTIVE_SECRETS_DIR,
        extra="ignore",
        case_sensitive=False,
        # A refusal is logged at boot; pydantic would otherwise append the whole
        # input (secrets included) to every validation error.
        hide_input_in_errors=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Read the dotenv files from the checkout root, not only the working
        directory, unless the caller named its own files."""
        if isinstance(dotenv_settings, DotEnvSettingsSource) and (
            dotenv_settings.env_file == ENV_FILE_NAMES
        ):
            dotenv_settings = DotEnvSettingsSource(settings_cls, env_file=env_files())
        return init_settings, env_settings, dotenv_settings, file_secret_settings


class Settings(SettingsSection):
    """Runtime configuration of the open platform, loaded from environment / .env."""

    # --- App ---
    app_env: AppEnv = "local"
    log_level: str = "INFO"
    # Expose a Prometheus /metrics endpoint on the backend + gateway (process +
    # RED request metrics). Off → no endpoint at all.
    metrics_enabled: bool = True
    # Bearer credential a scraper must present on GET /metrics
    # (`Authorization: Bearer <token>`; Prometheus' `bearer_token`/`bearer_token_file`).
    # Anonymous scrape is allowed ONLY in APP_ENV=local (loopback dev). In any other
    # environment /metrics answers 404 until this is set — the SaaS load balancer
    # routes on Host alone (no path filter, no reverse proxy in front of the ASGI
    # app), so an unauthenticated /metrics there is a public business-volume oracle
    # (request/error counts, latency, deploy times, the exact CPython version).
    # Setting it enforces it everywhere, local included. Empty == unset (the
    # alkera/<env> secret seeds every key as ""), i.e. no scrape credential.
    metrics_auth_token: str | None = None
    # Optional syslog/SIEM sink for structured logs. "host:port" (UDP by default;
    # prefix "tcp://" for TCP). Each redacted log event is also shipped here as a
    # JSON-payload syslog line. Empty → logs stay on stderr only.
    log_syslog_endpoint: str = ""

    # --- API ---
    api_host: str = "127.0.0.1"
    api_cors_origins: str = "http://localhost:5173"
    # How many trailing X-Forwarded-For hops were appended by OUR reverse-proxy
    # chain (1 = a single nginx/ALB in front, the SaaS default). The abuse
    # forensics read the client IP that many hops from the RIGHT: every proxy
    # appends the peer it actually saw, so only the left of the list is
    # client-forgeable. Deployments with a chained edge (e.g. CDN -> LB) raise
    # this to match their hop count.
    forwarded_for_trusted_hops: int = 1
    # Pre-buffer body cap for the gate CI endpoints the edge WAF exempts from its
    # 8 KB body rule (uploads + lookups; the path list is GUARDED_PATHS in
    # backend.api.body_limit). Enforced on the declared Content-Length BEFORE
    # FastAPI buffers the payload. Must stay above gate_service.MAX_ARTIFACT_BYTES
    # (32 MiB) plus envelope headroom, or valid artifacts get 413'd before the
    # service's own 422 ceiling can answer.
    gate_ingest_max_body_bytes: int = 33 * 1024 * 1024
    # How much of a JSON request body the boundary scan will hold in memory to
    # check it for text Postgres cannot store (see
    # alkera_core.validation.storable_text). It runs before routing and before
    # authentication on both apps, so the figure is a pre-auth memory bound per
    # in-flight request; a body past it is streamed through unscanned and the
    # driver-level net answers instead. A deployment whose clients post large
    # documents raises it to keep them covered; one that is memory-tight lowers
    # it.
    request_scan_max_body_bytes: int = 2 * 1024 * 1024
    # Hard ceiling on an org's LIVE (unrevoked, unexpired) CI tokens. Unlike the
    # in-process throttle above this is checked in Postgres, so it holds across
    # every task and across any burst length — the durable answer to "100 mints
    # in one second produced 100 usable credentials". Revoking frees a slot.
    gate_max_active_ci_tokens_per_org: int = 25

    # --- DB ---
    database_url: str = "postgresql+asyncpg://alkera:alkera@localhost:5432/alkera"
    database_url_sync: str = "postgresql+psycopg://alkera:alkera@localhost:5432/alkera"
    # Optional in-transit TLS to Postgres (off for local dev). Applies to BOTH the
    # async (asyncpg) app engine and the sync (psycopg) Alembic engine. Modes mirror
    # libpq: disable | prefer (default — try TLS, fall back) | require (encrypt, no
    # cert check) | verify-ca | verify-full. verify-* require DATABASE_SSLROOTCERT
    # (the server's CA, file path). See alkera_core.db.tls.
    database_sslmode: Literal["disable", "prefer", "require", "verify-ca", "verify-full"] = "prefer"
    database_sslrootcert: str | None = None
    # --- Migrations (`alembic upgrade`) ---
    # How long a revision waits for a table lock before it fails. An ALTER TABLE
    # that queues behind one long reader makes every later statement on that
    # table queue behind IT, so a short wait and a re-run beats a stalled fleet.
    migration_lock_timeout_ms: int = 5000
    # How long a second `alembic upgrade` waits for the first to finish before it
    # gives up. Every replica's init container runs the upgrade at once; all but
    # one wait here, then find the schema already at head and exit cleanly.
    migration_runner_wait_seconds: int = 900
    # Rows per committed statement when a revision back-fills a table by
    # primary-key range.
    migration_batch_rows: int = 5000
    # Async engine pool geometry (per PROCESS — every pooled task keeps
    # `pool_size` connections resident once warmed, `+ max_overflow` at peak).
    # The defaults suit the backend, which holds a connection for the whole
    # request. A service whose sessions are short bursts (the gateway: a few
    # millisecond-scale touches per stream) should run a smaller pool so the
    # fleet stays inside the managed instance's connection budget — set these
    # per-service in the task environment. See alkera_core.db.session.
    database_pool_size: int = 20
    database_pool_max_overflow: int = 10
    # How long one statement may WAIT FOR A LOCK, and how long it may RUN, on
    # every pooled connection (milliseconds; 0 switches the limit off). A request
    # holds a pooled connection for its whole life, and the pool is shared by
    # every tenant — so a statement parked behind someone else's row lock is a
    # connection nobody can use. Bounded, the blocked request fails in seconds
    # with a retryable 503 and hands its connection back; unbounded, a few dozen
    # of them are the whole pool and every route of every tenant fails. They are
    # sent as session defaults when a connection is opened, so nothing has to be
    # re-set on checkout and `RESET` returns to them. Alembic connects through
    # its own engine and takes neither. See alkera_core.db.session.
    database_lock_timeout_ms: int = 5_000
    database_statement_timeout_ms: int = 30_000
    # The same two limits for work nobody is waiting on a response for: the
    # worker process, and a Files operation run inline after its response. Long
    # enough for a batch statement over tens of thousands of rows; still finite,
    # so a wedged job ends instead of holding its locks until someone notices.
    database_background_lock_timeout_ms: int = 30_000
    database_background_statement_timeout_ms: int = 900_000
    # How long a connection may sit inside an open transaction running nothing
    # before Postgres ends its session (milliseconds; 0 switches it off). A task
    # that dies or hangs between two statements of a transaction otherwise
    # keeps that transaction, and every row lock it took, for as long as its
    # connection lives: every later writer of those rows waits out its lock
    # timeout and fails. Generous on purpose (a download or a model stream may
    # legitimately sit inside one for minutes), and finite, so a stuck holder
    # is ended rather than noticed. Production refuses 0 for requests.
    database_idle_in_transaction_timeout_ms: int = 600_000
    database_background_idle_in_transaction_timeout_ms: int = 1_800_000
    # A saturated pool is reported once per this many seconds, not per request.
    database_pool_pressure_log_interval_seconds: float = 10.0

    # --- Temporal (background work) ---
    # The Temporal frontend every service talks to: the backend starts / signals
    # workflows, the worker serves them, the health probe describes the namespace.
    # Self-hosted and SaaS both run `temporalio/auto-setup` next to the app on
    # plain gRPC; an external cluster (Temporal Cloud, a customer's own) is
    # reached with the optional API key / TLS knobs below.
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    # Empty == unset (the alkera/<env> secret seeds it as "").
    temporal_api_key: str | None = None
    # None → auto: TLS on iff an API key is set (a key must never travel in clear).
    temporal_tls: bool | None = None
    # Optional client mTLS: inline PEM text or a readable file path (same rule as
    # OUTBOUND_CA_BUNDLE), both-or-neither.
    temporal_tls_client_cert: str | None = None
    temporal_tls_client_key: str | None = None
    # The queues `python -m worker run` serves when `--queues` is omitted; the
    # SaaS task definitions pass one queue each, self-hosted serves all four.
    alkera_temporal_task_queues: str = "money,email,sync,default"
    # The worker's liveness endpoint (`GET /health/live`). Containers bind
    # 0.0.0.0 via the image ENV so k8s probes reach the pod IP; a bare process
    # stays on loopback.
    alkera_worker_health_host: str = "127.0.0.1"
    alkera_worker_health_port: int = 9000
    # Activities in flight per Worker (one per queue served). An advisory-locked
    # activity holds two connections (lock + session), so size this against
    # DATABASE_POOL_SIZE + DATABASE_POOL_MAX_OVERFLOW: 3 fits the 20+10 default;
    # the SaaS task definitions pin 1 next to their 2+1 pool.
    alkera_worker_max_concurrent_activities: int = 3
    # How long boot keeps retrying the first connect + namespace describe before
    # exiting 1. `auto-setup` registers the namespace seconds after the server
    # starts serving, so a worker that races it must wait, not crash-loop.
    alkera_worker_connect_timeout_seconds: int = 120

    # --- Realtime ---
    # One LISTEN connection per backend process fans committed outbox rows out
    # to in-process subscribers (the event stream, the doc-sync gateway). Off
    # only for a process that must never hold that one extra connection.
    realtime_listener_enabled: bool = True
    # How often the listener re-reads the outbox by cursor when no notification
    # arrived: the fallback that survives a lost NOTIFY, and its liveness probe.
    realtime_poll_interval_seconds: float = 5.0
    # Event-stream keepalive comment cadence; every tick re-checks the session.
    realtime_sse_keepalive_seconds: int = 15
    # A stream is closed (with a reconnect hint) after this long, so a rolling
    # deploy never waits on a connection that would otherwise live forever.
    realtime_sse_max_stream_seconds: int = 3000
    realtime_sse_max_streams: int = 500
    realtime_sse_max_streams_per_user: int = 8
    realtime_ws_max_connections: int = 500
    realtime_ws_max_connections_per_user: int = 8
    # The ceiling on one person and every workspace machine of theirs together.
    # A verified machine is counted on a key of its own (so a box never takes
    # one of its owner's slots), and this is what bounds how many such keys one
    # person's connections can spread across. Room for the person's own tabs
    # plus several boxes.
    realtime_sse_max_streams_per_principal: int = 32
    realtime_ws_max_connections_per_principal: int = 32
    # The memory one backend process may hold in websocket RECEIVE buffers at
    # peak, and the only place that peak is written down. uvicorn buffers a whole
    # incoming message, up to the launch line's `--ws-max-size`, before the
    # application sees a byte of it -- so the peak is the frame ceiling times the
    # number of sockets the process will hold, and raising either one without
    # raising this is how a process that fits its task definition stops fitting
    # it. `_validate_realtime_settings` multiplies them and refuses a boot whose
    # product exceeds this budget, so the container's `memory` has one number to
    # be sized against instead of two that multiply out of sight. A deployment
    # on a smaller task lowers BOTH this and the connection cap.
    # The default is EXACTLY the peak at the default cap, with no slack: raising
    # either number then has to be a deliberate act that names the memory it is
    # asking the container for, which is the whole point of writing it down.
    realtime_ws_frame_budget_bytes: int = 500 * REALTIME_WS_MAX_FRAME_BYTES
    # How many BYTES one socket may send in a frame window. The frame cap bounds
    # a single frame and the frame-count window bounds how often frames arrive,
    # but the two multiply: a socket inside both still pushes frame-cap x frame
    # budget of JSON per window, tens of megabytes this process then parses at
    # several times its wire size, and a user may hold
    # `realtime_ws_max_connections_per_user` of them. A person's socket sends a
    # subscribe, a presence beat and a cursor, so its budget is one whole frame
    # with room to spare; a verified machine publishing a chat streams a turn
    # through its socket and gets the larger one, for the same reason it gets
    # the larger frame window.
    realtime_ws_max_bytes_per_window: int = 4 * 1024 * 1024
    realtime_ws_publisher_max_bytes_per_window: int = 64 * 1024 * 1024
    # How many FRAMES a verified machine publishing a chat may send in a window.
    # A box coalesces a chat's stream into a frame every 50 ms and appends a
    # durable op per harness event on top, so 200 a window is what a QUIET turn
    # sends and a turn running several tools at once sends multiples of it. The
    # window is here to catch a socket looping on nothing, and closing a
    # publisher costs the turn every chunk in the reconnect gap — so it sits two
    # orders of magnitude above the cadence rather than beside it. The byte
    # budget above is what actually bounds the work this process does.
    realtime_ws_publisher_max_frames_per_window: int = 20_000
    realtime_ws_max_session_seconds: int = 3000
    # A socket ticket is minted for one handshake and dies unused after this.
    realtime_ws_ticket_ttl_seconds: int = 30
    # A presence row unheard from for this long is gone; it must outlast two
    # keepalive ticks or a healthy peer flickers.
    realtime_presence_ttl_seconds: int = 45
    # Ephemeral fan-out rides NOTIFY, whose payload Postgres caps at 8000 bytes.
    realtime_ephemeral_max_bytes: int = 4096
    # The whole state of one synchronised document.
    realtime_doc_max_bytes: int = 4 * 1024 * 1024
    # The largest payload a machine may deliver behind one promoted result
    # (POST /api/v1/objects/{id}/payload), measured on the request body. The
    # row ceiling bounds how many rows; this bounds how many bytes those rows
    # may put into the shared Postgres in one request. Over it is a 413.
    objects_payload_max_bytes: int = 16 * 1024 * 1024
    # The longest message a person may send in a chat. Over it the send is
    # refused and the typed message is lost, so this is sized far above anything
    # anyone writes or pastes rather than at what a message "should" be. It must
    # stay under what the event log's payload cap admits once the text is
    # JSON-escaped, which is the bound a send is actually refused by.
    chat_message_max_chars: int = 1_000_000
    # The most synchronised documents one org may hold of a single kind. A chat
    # document's id is chosen by the client, so without this ceiling one member
    # can mint rows without end, each able to grow to REALTIME_DOC_MAX_BYTES.
    # Counted per kind so a large knowledge base and a busy chat lane cannot
    # starve each other; past it a creating hello is refused in band as
    # `quota_exceeded` and every existing document keeps working.
    realtime_docs_max_per_org: int = 5000
    # The Loro sandbox: worker processes per backend process that validate CRDT
    # updates, so a Loro build that aborts on hostile bytes takes down a worker
    # rather than the process serving sockets. Each holds up to
    # `realtime_crdt_cache_docs` live documents in at most
    # `realtime_crdt_cache_bytes`, under an address-space cap of
    # `realtime_crdt_worker_memory_mb` (Linux enforces it; macOS does not).
    # Size the backend task's memory for workers x (~60 MB + the cache bytes).
    # A worst-case document (2 MiB of history) judging the largest update an
    # editor may send peaks near 75 MB resident on Linux, so 512 MB leaves a
    # wide margin while two processes' workers can never take the task down.
    realtime_crdt_workers: int = 2
    realtime_crdt_worker_memory_mb: int = 512
    realtime_crdt_cache_docs: int = 256
    realtime_crdt_cache_bytes: int = 64 * 1024 * 1024
    # The interpreter the sandbox workers run on; empty is the backend's own.
    # The notebook format code runs in the workers, so a deployment points
    # this at a Python at least as new as the newest Python a notebook's
    # environment may use (3.14), whose environment holds `loro`, the backend
    # sandbox package and `alkera-notebook` (`make nb-doc-sandbox-python`).
    realtime_crdt_worker_python: str = ""
    # How long a worker has to judge one update, and to rebuild a document
    # from its snapshot and log, before it is killed and replaced.
    realtime_crdt_validate_timeout_ms: int = 2000
    # Whether a replica sweeps for live file sessions holding edits not yet
    # written to the drive (at start and every few seconds) and writes them
    # back. Always on in a deployment: it is what writes back the edits of a
    # process that stopped between an edit and its write back. The test suite
    # turns it off, because a sweep reaches every session in the database and a
    # worker database holds every earlier test's; a test that needs the sweep
    # starts one scoped to its own org.
    realtime_crdt_unsaved_sweep_enabled: bool = True
    realtime_crdt_load_timeout_ms: int = 10000
    # Whether people may open files live (co-edited, saved as they type) and a
    # box may merge its agent's edits into them as a text peer. Off, a file
    # opens read-only in the browser and a box writes the file the ordinary
    # way; sessions already holding typing are written back first, and the
    # unsaved sweep keeps writing back whatever is left. An org's own setting
    # (set by platform admins) wins over this in either direction.
    live_editing_enabled: bool = True

    # --- Observability ---
    # Sentry stays a COMPLETE no-op until SENTRY_DSN is set.
    # When enabled, the two settings below tune it; `sentry_environment` defaults
    # to APP_ENV. The CLI/daemon resolve their own DSN (they don't read this .env).
    sentry_dsn: str | None = None
    sentry_environment: str | None = None
    sentry_traces_sample_rate: float = 0.0

    # --- AWS (prod) ---
    aws_region: str = "us-east-1"
    # Bedrock auth — two options, both for local dev (`.env.local`); LEAVE UNSET
    # in prod (the gateway then uses the standard credential chain / IAM role):
    #  1. A Bedrock API key (the single `ABSK...` string from the console). The
    #     gateway hands it to its own instance-credential client only, never to
    #     the process environment, so it cannot sign an org's BYOK request.
    #  2. A classic IAM access-key pair (passed to the bedrock-runtime client).
    aws_bearer_token_bedrock: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_session_token: str | None = None

    # --- Mail ---
    # Local default is Mailpit on :1025 (no auth, no TLS). Any production relay
    # (SES, Resend over smtp.resend.com:587/STARTTLS, …) needs SMTP_USERNAME,
    # SMTP_PASSWORD, and SMTP_USE_TLS=true; the prod-mode validator below refuses
    # to start a production app pointed at "localhost" or without SMTP credentials.
    # Master switch for outbound email. Default True. Set EMAIL_ENABLED=false for a
    # no-email / air-gapped deployment: the app boots with NO SMTP relay, every send
    # becomes a logged no-op, and email-gated flows degrade gracefully (see
    # the deployment guide) — email verification is treated as satisfied (a link
    # can't be delivered), and users see + accept team invitations in-app instead of
    # via an emailed link. Self-serve password reset is unavailable with no email, so
    # a no-email deployment is expected to use SSO (recommended) or admin-set creds.
    email_enabled: bool = True
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_from: str = "no-reply@alkera.local"
    # Display name attached to the From header, e.g. `Alkera AI <no-reply@…>`.
    # Empty string sends a bare address (no display name).
    # Unset: the brand's sender name (``alkera_core.brand.sender_name``).
    smtp_from_name: str | None = None
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = False
    smtp_timeout_seconds: int = 10

    # --- Branding / white-label ---
    # Customer-facing product name + support address, surfaced in the SPA (via the
    # public /api/v1/config endpoint) and in transactional email. A self-hosted
    # deployment overrides these to run under its own brand without touching code or
    # rebuilding images. Unset (or empty): the name of the brand the build registers
    # (``alkera_core.brand.product_name``). The two addresses fall back to the
    # registered brand's, and the open build's brand names none.
    brand_product_name: str | None = None
    brand_support_email: str | None = None
    # Who to write to about an Enterprise plan, named where an org meets a surface
    # its plan does not include.
    brand_sales_email: str | None = None
    # The transactional-email header wordmark. Email clients can't render SVG or the
    # app's web fonts, so a logo has to be a hosted raster (PNG) the mail client
    # fetches at open time — set this to its absolute URL. Opt-in: unset (or empty) →
    # NO image, the header degrades to the product-name text wordmark. The web app
    # serves its brand's wordmark under ``/email/`` (Databench's is
    # ``/email/databench-wordmark.png``), so a deployment points this at its own host
    # and never hotlinks another's asset.
    brand_email_logo_url: str | None = None
    # The address the welcome email is sent from and replied to, so a new user's
    # reply reaches a person. Unset (or empty): the registered brand's, else it goes
    # out from ``smtp_from`` with replies to the support address. The domain must
    # pass SPF/DKIM/DMARC.
    email_founders_from: str | None = None
    email_founders_from_name: str | None = None

    # --- Versioning / ops ---
    # Both are injected at deploy time (Docker label, CI build, etc.). They
    # surface via /health/info so ops can verify which build is live.
    app_version: str = "0.0.0"
    build_id: str | None = None
    # How long /health/ready waits for the database to answer `SELECT 1`, in
    # every service that serves the endpoint. Matched to the load balancer's own
    # health-check timeout: a probe that gives up sooner than the thing reading
    # it pulls a task out of rotation for a database that is slow rather than
    # gone, and a busy-but-serving Postgres then drains every task at once.
    # /health/live never consults it — liveness is unconditional.
    health_ready_timeout_seconds: float = 5.0
    # How long a task that has been ready keeps answering /health/ready with 200
    # while it cannot reach the database or the object store, measured from its
    # last fully ready probe. The load balancer's check is also what makes ECS
    # replace a task, and a dependency every task shares fails every task at
    # once: replacing them fixes nothing and leaves the target group empty. The
    # body still says "degraded" throughout. A task that has never been ready
    # gets no grace, so a broken new revision still fails its health check and
    # rolls back. 0 is the strict probe. See alkera_core.readiness.
    health_ready_grace_seconds: float = 900.0

    # --- Frontend ---
    # Where the SPA is served. Used to build invitation accept links.
    frontend_base_url: str = "http://localhost:5173"

    # --- Auth ---
    # A directory this install keeps (a volume every process mounts). When set,
    # AUTH_JWT_SECRET, TOKEN_HASH_PEPPER and FILES_CONTENT_SIGNING_KEY left unset
    # are generated once and persisted there (mode 0600), and SECRET_BOX_KEY with
    # them on a fresh install. Unset, nothing is generated and production refuses
    # a missing secret. Never point replicas without shared storage at it.
    generated_secrets_dir: str | None = None
    # JWT signing secret. MUST be set in non-local environments.
    auth_jwt_secret: str | None = None
    # Retired JWT secrets kept ONLY to verify already-issued tokens during a
    # graceful rotation (comma-separated). New tokens always sign with
    # AUTH_JWT_SECRET; decode tries the active secret then each of these, so
    # rotating no longer logs every session out. Drop a value once no live token
    # could still bear it (after auth_cli_token_ttl_seconds for the longest case).
    auth_jwt_secret_previous: str = ""
    auth_jwt_alg: str = "HS256"
    # Pepper for keyed-hashing single-use lookup tokens (password-reset,
    # email-verification, invitation) before they're persisted, so a DB read
    # can't replay the emailed link. MUST be set in non-local environments (see
    # `_validate_auth`). Mirrors AUTH_JWT_SECRET; sourced from the same secret.
    token_hash_pepper: str | None = None
    # Retired peppers kept ONLY for LOOKUP during a graceful pepper rotation
    # (comma-separated). New digests are written with TOKEN_HASH_PEPPER; reads
    # match the active digest OR any of these, so rotating doesn't invalidate
    # outstanding reset/verify/invite links or long-lived proxy tokens. Drop a
    # value once nothing persisted could still hash under it.
    token_hash_pepper_previous: str = ""
    # Access-token TTL (seconds) — the JWT `exp` of the browser session cookie,
    # fixed at mint and never extended. 1800 = 30 minutes. The JWT is stateless
    # between revocation checks, so this bounds how long a STOLEN cookie works on
    # its own; the browser stays signed in past it by exchanging the rotating
    # refresh cookie (below) for a fresh access token, which the server can
    # refuse at any time. Production refuses a value above one hour
    # (`_validate_production`).
    auth_token_ttl_seconds: int = 1800
    # One identity in several orgs. With it off, every credential door refuses an
    # org that is not the identity's home org (the single-org guard). Turning it on
    # is safe because the tenancy ratchets in apps/backend/tests (no org read off a
    # user row, no membership query without an org) hold at zero findings.
    multi_org_enabled: bool = False
    # What happens to a write on the browser's session cookie that does not name
    # the org its tab rendered (`X-Alkera-Org`). `log`: it goes through and an
    # `org_assertion.missing` warning (route, method, org id) is logged. `enforce`:
    # it is refused with a 428 before the route runs. Run `log` until that event
    # has dropped to zero (every open tab runs a portal that names its org on
    # every write), then switch to `enforce`. Other credentials are never held to
    # it, and a header naming another org is refused in both modes.
    org_assertion_mode: Literal["log", "enforce"] = "log"
    # Stricter enforcement for an org that requires single sign-on, independent
    # of multi-org. Off: an enforced org refuses a password, Google or GitHub
    # sign-in of a person it governs, and a session it admitted stays signed in
    # for its normal lifetime. On: every refresh also needs a sign-in through
    # the org's IdP within the connection's `session_max_age_seconds`, a
    # membership the org's SCIM provisioned counts as governed, and turning
    # enforcement on ends the governed members' CLI tokens and access keys.
    sso_strict_enforcement_enabled: bool = False
    # How many orgs one identity may create in any 30-day window, counted per
    # person, not per org.
    max_org_creations_per_identity_30d: int = 3
    # Who may create an account that founds a NEW org (public signup, or an
    # OAuth sign-up with no invitation). `open`: anyone who reaches the portal.
    # `invite_only`: only a person an org admin invited; the first admin comes
    # from `bootstrap-admin` or the seeds. Unset, a deployment that says it is
    # self-hosted (SELF_HOSTED=true) is invite-only: every org there runs on the
    # operator's provider keys and reaches the operator's network, so a stranger
    # who could sign up would get both.
    signup_mode: Literal["open", "invite_only"] | None = None
    # The longest access-token TTL a production deployment may configure. A
    # stateless credential that outlives an hour is the finding the refresh
    # token exists to close, so a larger value is a misconfiguration, not a policy.
    auth_token_ttl_production_max_seconds: int = 3600
    # The refresh token behind a browser session: an opaque 256-bit secret in an
    # HTTP-only cookie scoped to the refresh route, stored only as a keyed hash,
    # rotated on every use. It dies of neglect after `auth_refresh_idle_seconds`
    # without a refresh (7 days: nobody loses today's "stays signed in for a
    # week") and unconditionally at `auth_refresh_absolute_seconds` after login
    # (30 days), whatever the activity. Both are server-side rows, so a family
    # is revocable at any moment: logout, password change, ban, a role change.
    auth_refresh_idle_seconds: int = 60 * 60 * 24 * 7
    auth_refresh_absolute_seconds: int = 60 * 60 * 24 * 30
    # A rotated token presented AGAIN is theft: the whole family is revoked.
    # The exception is a re-presentation within this many seconds of the
    # rotation while its child is still current, which is two tabs of one
    # browser refreshing at once; it is handed the SAME child, never a second.
    # Ten seconds covers two racing requests on a slow network plus server
    # queueing, and keeps short the window in which a copied token rides a
    # legitimate rotation (it only ever obtains the same child, so the next
    # divergent use is caught). 0 makes detection strict; production refuses
    # more than AUTH_REFRESH_REUSE_GRACE_MAX_SECONDS.
    auth_refresh_reuse_grace_seconds: int = 10
    auth_refresh_cookie_name: str = "alkera_refresh"
    # An idle window on the ACCESS token itself, sliding on every use (0 = off;
    # the hosted deployment pins 86400). It is the narrower of the two idle
    # windows and only bites when set below the access TTL; the window that
    # normally ends an unattended browser is the refresh family's above. An open
    # event stream or socket slides it too — a held connection is a person
    # working — which is why the absolute lifetime, and not this, is what ends a
    # tab left open. Production refuses a value at or above the absolute lifetime.
    auth_idle_timeout_seconds: int = 0
    # CLI tokens are long-lived JWTs (90 days) issued by the device-authorization
    # token endpoint (/auth/device/token) for the `alkera` CLI + VS Code
    # extension. Same shape as the session token; just a longer expiry so users
    # don't re-auth weekly.
    auth_cli_token_ttl_seconds: int = 60 * 60 * 24 * 90  # 90 days
    # Minimum length for a password a USER chooses (signup, reset, profile
    # edit). Enforced at the route layer by `backend.auth.password_policy`,
    # which also refuses a password derived from the account's own email or
    # name — the failure a length rule alone can never catch. Operator tooling
    # (`make seed`, `bootstrap-admin`) goes through the service layer and is
    # deliberately not governed by it.
    auth_password_min_length: int = 12
    # Which argon2id work factor `backend.auth.password` hashes at. `production`
    # is the real cost and the only value a deployment may run (see
    # `_validate_production`); `fast` drops the work factor to argon2's floor so a
    # test suite that creates users and logs them in per test stops paying ~70 ms
    # per hash for a barrier no test is measuring. Verification is the same real
    # argon2 verify under both, and argon2 encodes its parameters in the hash, so
    # switching profiles never invalidates a stored credential.
    # (S105 reads the field name and calls the value a hardcoded password; it is
    # the name of a cost profile.)
    password_hash_profile: PasswordHashProfile = "production"  # noqa: S105
    # `Cross-Origin-Resource-Policy` for the API responses. `same-site` fits the
    # SaaS shape (app.example.com + api.example.com share a registrable domain); a
    # self-hosted deployment that splits the SPA and API across unrelated
    # domains sets `cross-origin`. CORP governs no-cors subresource loads only,
    # so neither value affects the SPA's CORS `fetch` traffic.
    security_cross_origin_resource_policy: str = "same-site"
    # --- Request throttling ---
    # Every route of the API resolves to one rate-limit CLASS (see
    # backend/api/rate_limit.py): a burst the caller may spend at once and a
    # sustained per-minute rate, counted per class per key. The edge WAF
    # aggregates over five minutes, so a one-second burst never reaches its
    # limit — these are the near-endpoint half of that pair. The buckets are
    # in-process and therefore per-task, so the deployment-wide ceiling is
    # `limit x task count`; the two strict classes (credential, mint) carry a
    # durable hourly window in Postgres on top, which holds across every task.
    # Production refuses to run with this off; a self-hosted deployment fronting
    # the app with its own throttle may turn it off outside production.
    rate_limit_enabled: bool = True
    # `credential`: login, signup, password reset, email verification, the
    # device grant, CLI tokens, OAuth / SSO starts. Keyed by the ACCOUNT the
    # attempt names (an email, a token, a device code) AND by the caller IP —
    # both must pass. The account leg sits above the failure-aware lockout
    # (auth_lockout_threshold: 5 failures lock for 15 minutes) so the lockout
    # answers a brute force and this answers volume; the IP leg is sized for a
    # shared egress address — a corporate NAT puts a whole office behind one
    # key, so it has to clear a Monday-morning login spike.
    rate_limit_credential_per_minute: int = 20
    rate_limit_credential_burst: int = 10
    rate_limit_credential_per_hour: int = 60
    rate_limit_credential_ip_per_minute: int = 60
    rate_limit_credential_ip_burst: int = 30
    rate_limit_credential_ip_per_hour: int = 600
    # `device_poll`: the device grant's `/token` poll. Polling is the protocol —
    # the client repeats it at `auth_device_poll_interval_seconds` for as long as
    # the code lives — so it cannot share `credential`, whose numbers are sized
    # for a person typing a password: one patient login would spend the minute,
    # the hour, and the address's budget for starting the next one. Pace per
    # code is the service's `slow_down`; this bounds volume. No hourly leg: the
    # code is 256 random bits, so there is no guessing to bound, and a durable
    # count would be a Postgres write on every poll. The per-code minute must
    # clear the issued cadence (validated below); the address leg is sized for
    # an office signing in together behind one NAT.
    rate_limit_device_poll_per_minute: int = 90
    rate_limit_device_poll_burst: int = 30
    rate_limit_device_poll_ip_per_minute: int = 900
    rate_limit_device_poll_ip_burst: int = 300
    # `mint`: CI tokens, personal access tokens, API keys — the class of call
    # an external assessment drove 100 times in one second. Per principal.
    rate_limit_mint_per_minute: int = 10
    rate_limit_mint_burst: int = 10
    rate_limit_mint_per_hour: int = 60
    # `mutation`: POST / PUT / PATCH / DELETE on ordinary resources (KB items,
    # teams, invitations, connections, settings). Per principal.
    rate_limit_mutation_per_minute: int = 120
    rate_limit_mutation_burst: int = 40
    # `read`: GET / HEAD. Per principal (the caller IP when anonymous).
    rate_limit_read_per_minute: int = 600
    rate_limit_read_burst: int = 120
    # `chat`: prompt send, turn control, chat create / rename, attachments,
    # message reads. A person typing fast, a Slack thread and a webview polling
    # must never see a refusal. Per principal.
    rate_limit_chat_per_minute: int = 300
    rate_limit_chat_burst: int = 100
    # `upload`: Files upload-session opens, inline content writes, tree and
    # child creates, the drag-and-drop batch. A 500-file folder drop at three in
    # flight opens ~35 sessions a second for ~15 s, faster than any refill, so
    # the burst holds a whole drop. Per principal. Part PUTs and completes are
    # bounded by the session that admitted them (`upload_part`), not here.
    rate_limit_upload_per_minute: int = 1200
    rate_limit_upload_burst: int = 600
    rate_limit_upload_part_per_minute: int = 6000
    rate_limit_upload_part_burst: int = 3000
    # Reading an operation back: one poll per queued commit, copy or download
    # the principal is following. Its load scales with the work the principal
    # queued -- a 500-file drop polls 500 commits -- so it is sized like the
    # parts rather than like a person browsing, and kept off the read budget
    # the drop's folder listings still need.
    rate_limit_operation_per_minute: int = 6000
    rate_limit_operation_burst: int = 3000
    # `stream_open`: the SSE event stream and socket handshakes, counted at OPEN
    # only — frames and keepalives never count. Reconnect storms are absorbed
    # by the client's backoff floor (2 s). Per principal.
    rate_limit_stream_open_per_minute: int = 60
    rate_limit_stream_open_burst: int = 20
    # `ws_ticket`: the single-use socket ticket mint — its own budget, kept.
    rate_limit_ws_ticket_per_minute: int = 30
    rate_limit_ws_ticket_burst: int = 30
    # `machine`: the workspace box's heartbeat, registration, Files push / delta
    # and lease hand-back, and the daemon's audit and inventory flushes. Keyed by
    # the machine the request asserts once that assertion verifies as a machine
    # the caller registered on the very session it carries, so a busy box never
    # starves its owner's browser; a push of 300 deltas lands within seconds. An
    # assertion that does not verify is counted as the person. The same numbers
    # size each (machine, resource) budget of the `agent` class.
    rate_limit_machine_per_minute: int = 900
    rate_limit_machine_burst: int = 400
    # The ceiling on everything one principal's machines do together, on both
    # the `machine` and the `agent` class: however many chats and folders its
    # boxes name, one person's machines never exceed it. Keyed on the person,
    # not the address, so boxes that share one egress never share a budget.
    # Sized for several boxes, each at its own steady rate.
    rate_limit_agent_principal_per_minute: int = 9_000
    rate_limit_agent_principal_burst: int = 4_000
    # `box`: a workspace box speaking on its own machine credential: the
    # Files reads, uploads, parts and progress polls its folder sync makes. A
    # box syncing an agent's burst makes several requests per file, so the
    # person-sized `read`/`upload` budgets throttled it against its own API
    # (1,500 files left 400 behind after 20 minutes). Keyed by the credential,
    # with the caller address as the ceiling, so a forged credential string
    # cannot buy a fresh budget per request.
    rate_limit_box_per_minute: int = 6_000
    rate_limit_box_burst: int = 3_000
    # `lease_node`: the live plane a box runs over one leased folder. Keyed by
    # (org, machine, folder) rather than by the machine, because a box holds one
    # lease per chat and the chat writing hardest must not spend the budget the
    # others report on. A batch every `files_live_batch_ms` is ~120 a minute per
    # folder, so this is five times the steady rate.
    rate_limit_lease_node_per_minute: int = 600
    rate_limit_lease_node_burst: int = 300
    # The ceiling on the same class. The folder in the per-folder key is a path
    # segment the caller writes, so a caller naming a new folder on every
    # request spends no per-folder budget at all; the address is the one part of
    # the key nothing in the request can rotate. Sized for the boxes behind one
    # address — several holders, each reporting per folder — not for one of them.
    rate_limit_lease_node_ip_per_minute: int = 6_000
    rate_limit_lease_node_ip_burst: int = 3_000
    # `lease_tree`: the holder's tree report -- every name, kind, size and
    # modified time under a leased folder, ahead of the bytes. Keyed per folder
    # like `lease_node`, and a class of its own so a clone's burst of batches
    # never spends the budget the folder's live reports run on. A holder flushes
    # at most every `files_live_metadata_every_ms`, so 240 a minute is a batch
    # every quarter second held for a whole minute.
    rate_limit_lease_tree_per_minute: int = 240
    rate_limit_lease_tree_burst: int = 120
    # The ceiling on the same class, keyed by address for the reason the
    # `lease_node` ceiling is: the folder in the key is a path segment.
    rate_limit_lease_tree_ip_per_minute: int = 2_400
    rate_limit_lease_tree_ip_burst: int = 1_200
    # `webhook`: the budget of one tenant a VERIFIED delivery names -- a Slack
    # workspace, a Stripe account, a GitHub installation -- charged by the route
    # after the signature checks, never from an unverified body. Sized so a
    # signed delivery is accepted and drained rather than refused into a
    # provider retry storm.
    rate_limit_webhook_per_minute: int = 600
    rate_limit_webhook_burst: int = 300
    # The only budget a webhook request spends BEFORE its signature is checked:
    # one per (route, source address). A provider posts every tenant's
    # deliveries from a shared pool of addresses, so this is sized for the whole
    # fleet's traffic through one of them, not for one tenant.
    rate_limit_webhook_ip_per_minute: int = 6_000
    rate_limit_webhook_ip_burst: int = 3_000
    # Signature failures per (provider, source address), charged only after
    # the signature has failed: a failure answers 400, and 429 once the source
    # has spent this. A valid signature never reads it, so forgeries sent from
    # a provider's shared address pool cannot shut out genuine deliveries.
    rate_limit_webhook_reject_per_minute: int = 60
    rate_limit_webhook_reject_burst: int = 30
    # `admin`: the platform-staff routes. Per principal.
    rate_limit_admin_per_minute: int = 120
    rate_limit_admin_burst: int = 60
    auth_cookie_name: str = "alkera_session"
    # `Secure` cookie flag — must be True in production (HTTPS only).
    auth_cookie_secure: bool = False
    # Dev admin seeded by `make seed` when APP_ENV is local or staging. No effect
    # in production.
    auth_dev_admin_email: str = "admin@example.com"
    # The seeded admin's name and the org the seed creates for them.
    auth_dev_admin_first_name: str = "Dev"
    auth_dev_admin_last_name: str = "Admin"
    auth_dev_org_name: str = "Dev org"
    # The dev admin password is intentionally weak; the seed only runs in local or
    # staging, never production. Keep staging access-controlled.
    auth_dev_admin_password: str = "admin"  # noqa: S105
    # Days a freshly-created account may use the product before its email MUST be
    # verified. After this window an unverified account is blocked from the API +
    # model gateway (the SPA shows a full-screen "verify your email" gate). The
    # policy lives in `alkera_core.verification`; verified, invited, and
    # platform-staff accounts are never gated.
    email_verification_grace_period_days: int = 7
    # Minimum gap between two self-service verification emails for the same
    # account. Long enough to stop a session from flooding a mailbox (and
    # burning the account-wide SES quota), short enough that a user whose first
    # email went to spam isn't stranded. Clients read the derived
    # `verification_resend_available_at` on /auth/me to render a countdown.
    email_verification_resend_cooldown_seconds: int = 300

    # --- Device Authorization Grant (RFC 8628) ---
    # The `alkera` CLI + VS Code extension authenticate via the device flow: they
    # request a device_code + short user_code, the user approves in a browser on
    # any device, and the client polls the token endpoint until approval.
    # Brute-force lockout on the password login path (DB-backed; for self-hosted
    # deployments without an edge WAF). Lock after THRESHOLD consecutive failures
    # within WINDOW seconds, for DURATION seconds. THRESHOLD=0 disables lockout.
    auth_lockout_threshold: int = 5
    auth_lockout_window_seconds: int = 900
    auth_lockout_duration_seconds: int = 900
    # How long each person's identity security log is kept, in days. A daily
    # worker job deletes older rows (every failed sign-in writes one); the
    # platform's disable and re-enable decisions are kept regardless.
    identity_security_event_retention_days: int = Field(default=365, ge=1)
    # Extra origins allowed to make a COOKIE-authenticated state change, on top
    # of `frontend_base_url` and `api_cors_origins` (comma-separated, scheme +
    # host + optional port). A session cookie travels to every origin under the
    # registrable domain, so the origin guard admits only what is listed here;
    # a deployment that serves the portal from a second hostname (a vanity
    # domain, a reverse proxy under another name) adds it here rather than
    # widening CORS, which would also grant those origins read access.
    auth_csrf_trusted_origins: str = ""
    # How long an unredeemed device_code/user_code stays valid (seconds). Kept
    # short because the user_code is human-typed and low-entropy.
    auth_device_code_ttl_seconds: int = 600
    # Minimum seconds the client must wait between token-endpoint polls. Returned
    # as `interval` in the device/code response and enforced server-side
    # (slow_down). 1s gives a snappy ~sub-minute login without hammering.
    auth_device_poll_interval_seconds: int = 1
    # How long a member's lease of a team connection's shared service credential
    # stays valid (seconds). The daemon holds it in memory and re-leases on
    # expiry, so this is the window in which a disabled or deleted connection can
    # still be used — the shorter it is, the more backend round-trips a working
    # member pays. Rotation is the immediate kill switch; this bounds the rest.
    team_connection_lease_ttl_seconds: int = 900
    # Number of characters in the generated user_code, excluding the group dash.
    # Must be even (rendered as two equal groups, e.g. WXYZ-1234). 8 chars from a
    # 32-symbol ambiguity-free alphabet is ~40 bits.
    auth_device_user_code_length: int = 8
    # Hard cap on token-endpoint polls for one device_code before it's
    # force-expired — bounds an attacker polling guessed device_codes. TTL +
    # interval already bound legitimate polls (~600 at a 1s cadence).
    auth_device_max_poll_attempts: int = 1000
    # Max failed user_code lookups (info/approve/deny) per authenticated session
    # before lockout, guarding the low-entropy user_code against online brute
    # force (RFC 8628 §5.1/§5.2).
    auth_device_max_user_code_attempts: int = 10

    # --- Signup policy ---
    # Comma-separated EXTRA personal/free email-provider domains to treat as
    # non-business at signup, on top of the backend's vendored default list
    # (apps/backend/backend/auth/data/free_email_domains.txt). Self-hosted
    # deployments use this to extend coverage without editing code. The union
    # (defaults + these extras) lives in backend.auth.email_policy — this is just
    # the raw config knob. Comments-on-own-line rule applies (see CLAUDE.md):
    # never put a comment on the same line as the value.
    personal_email_domains: str = ""

    # --- Alkera workspace ---
    # Filesystem root that holds `.alkera/graphs/`, `.alkera/runs/`, etc.
    # When unset, services fall back to `Path.cwd()` (developer running
    # `make dev-backend`). Tests override this directly via monkeypatch.
    alkera_project_root: Path | None = None

    # --- External login (OAuth / OIDC) ---
    # Per-provider client credentials. A provider's sign-in button only appears
    # (and its routes only work) when BOTH id + secret are set. Put real values
    # in gitignored `.env.local`. The prod validator below enforces id↔secret
    # pairing and that the mock provider is off in production.
    oauth_google_client_id: str | None = None
    oauth_google_client_secret: str | None = None
    oauth_github_client_id: str | None = None
    oauth_github_client_secret: str | None = None
    # The mock provider lets the full start→callback→register flow run locally
    # (and in tests) with zero external calls. Defaults to FALSE so it is
    # fail-safe: it requires TWO positive signals to ever register — `is_local`
    # (APP_ENV=local) AND an explicit OAUTH_MOCK_ENABLED=true. A prod deploy that
    # forgets APP_ENV (so it falls back to "local") still won't expose the mock,
    # because prod has no `.env` setting this flag. Local dev opts in via
    # `.env`/`.env.local` (see `.env.example`); CI's `test` job sets it too.
    oauth_mock_enabled: bool = False
    # The externally reachable backend origin: what OAuth providers redirect
    # back to (backend routes hit directly by the user's browser, NOT the SPA
    # origin) and what the gate's status endpoint reports as the API URL CI
    # runners upload reports to (the generated workflows embed it). Self-hosted
    # deployments override it; `make gate-tunnel` points it at a dev tunnel.
    api_public_base_url: str = "http://localhost:8000"

    # --- Bot protection (Cloudflare Turnstile) ---
    # Secret key for server-side verification of the invisible Turnstile challenge
    # the SPA attaches to login / signup / password-reset. When unset, the captcha
    # check is a complete no-op (so local dev and the test suite run without it);
    # set it (in `.env.local` locally, the deployment's secret store otherwise) to enforce.
    # The public SITE key is frontend-only (Vite's VITE_TURNSTILE_SITE_KEY) — the
    # backend never needs it. The prod validator below requires this in production.
    turnstile_secret_key: str | None = None
    # Local/staging opt-in: even WITH a secret key set, the captcha stays OFF in any
    # non-production env unless this is true — so a `.env.local` test key can't make
    # every local login fail. Set TURNSTILE_DEV_ENABLED=true to deliberately exercise
    # the full challenge flow in dev/staging. Ignored in production, where the key
    # alone enforces (and the prod validator makes it mandatory).
    turnstile_dev_enabled: bool = False
    # Whether Turnstile bot-protection is REQUIRED in production. SaaS leaves this
    # True — a public signup surface must be bot-guarded, so prod refuses to boot
    # without a secret key. Self-hosted / VPC / air-gapped deployments (internal
    # signup surface, frequently no egress to challenges.cloudflare.com) set
    # TURNSTILE_REQUIRED=false to run with NO captcha and make NO Cloudflare calls.
    # When false the captcha is fully off regardless of any key, and the prod
    # validator no longer demands one.
    turnstile_required: bool = True

    # --- Model gateway / LLM providers ---
    # The gateway verifies the user's Alkera JWT (shared `auth_jwt_secret`) and
    # swaps in the real provider credentials server-side. Base URLs are
    # overridable so tests can point at mock servers. Bedrock uses `aws_region`
    # + the SigV4 credential chain (IAM role). The gateway validates the creds
    # it actually needs at its own startup — they're not globally prod-required
    # (the backend doesn't hold provider keys).
    anthropic_api_key: str | None = None
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_version: str = "2023-06-01"
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    bedrock_base_url: str | None = None  # endpoint_url override (tests)
    # --- Per-STEP gateway bounds -------------------------------------------
    # Every bound below is PER MODEL STEP, NOT PER TURN. A turn is many steps and
    # may legitimately run for days; one step is entitled to run for a day. They
    # stay finite only because the credit-reservation sweeper needs an outer
    # bound: a hold whose stream never ends is reclaimed, and a settle after that
    # reclaim no-ops on the claim guard (served but never billed). So the
    # defaults are generous rather than absent — raise them freely, but never to
    # "unlimited".
    #
    # Generous silence bounds are NOT how a crashed upstream is caught. A peer
    # that dies without closing the socket is detected by the TCP keepalive
    # probes armed on the outbound transport (alkera_core.http), in minutes,
    # whatever these are set to — which is what lets them be this large.
    #
    # How long an HTTP provider may go SILENT mid-response before the gateway
    # gives up on the stream (httpx read timeout, seconds). PER MODEL STEP, NOT
    # PER TURN. A thinking step streams deltas, but a provider that buffers a
    # very long reasoning block sends nothing at all while it thinks, so the
    # bound is sized for that, not for a keepalive cadence.
    gateway_upstream_read_timeout_seconds: float = 7200.0
    # The same bound for Bedrock, which streams over botocore rather than httpx
    # and whose own inference timeout is far longer than boto's 60s default.
    # PER MODEL STEP, NOT PER TURN.
    gateway_bedrock_read_timeout_seconds: int = 86400
    # Wall-clock cap on ONE streamed model step before the sweeper reclaims its
    # reservation (seconds). PER MODEL STEP, NOT PER TURN. The pipeline cuts the
    # stream a minute earlier so its settle lands before the sweeper's cutoff. At
    # parity with the upstream read timeouts above this cap is the tighter bound
    # and cuts first — which is the safe order, because a cut here still settles
    # the usage that streamed.
    gateway_max_stream_seconds: int = 86400
    # How often the gateway writes an SSE comment to the CLIENT while the
    # upstream is silent (seconds; 0 disables it outside production). A silent
    # step can now outlast any edge idle timeout, and an ALB/proxy/CDN that sees
    # no bytes for its own window closes the connection — cutting a step the
    # gateway and the provider both consider healthy. A comment is invisible to
    # every conformant SSE consumer, so it costs the transcript nothing.
    gateway_client_keepalive_seconds: float = 15.0
    # The idle timeout of whatever sits between the client and the gateway (the
    # SaaS ALB's `idle_timeout`, or a customer's proxy / CDN). Nothing reads it
    # at runtime — it exists so the production validator can refuse a keepalive
    # cadence that would not actually keep the edge's connection alive.
    gateway_edge_idle_timeout_seconds: float = 4000.0
    # Backpressure: max concurrent in-flight provider streams PER GATEWAY PROCESS.
    # The gateway sheds with 429 + a `gateway.backpressure.shed` log past this.
    # It MUST stay at or below UPSTREAM_POOL_MAX_CONNECTIONS: every in-flight
    # request holds one connection of the gateway's single shared httpx client, so
    # a higher cap never sheds — instead the pool saturates and requests fail with
    # a 10s PoolTimeout that the pipeline retries and fails over, multiplying the
    # time each admitted request pins a gate slot, a credit reservation and a DB
    # row. Headroom below the pool ceiling absorbs redirects / connection churn.
    gateway_max_concurrent_streams: int = 80
    # If the gateway relies SOLELY on a Bedrock IAM role / instance profile (no
    # Anthropic/OpenAI/Bedrock key in env), set this true so the gateway's
    # startup provider-config check doesn't refuse to boot. Default false.
    gateway_assume_bedrock_iam: bool = False
    # Where the BACKEND reaches the model gateway for its deployment health check
    # (the SPA/CLI discover the gateway their own way). Default = the dev port;
    # in compose/helm point it at the gateway service (e.g. http://gateway:8081).
    gateway_base_url: str = "http://localhost:8081"

    # --- Egress / outbound TLS (locked-down VPCs) ---
    # A corporate forward proxy for ALL outbound calls (providers, OAuth/OIDC, JWKS,
    # captcha) is honored automatically from the standard HTTP_PROXY / HTTPS_PROXY /
    # NO_PROXY env vars — httpx reads them via trust_env, which we never disable — so
    # no setting is needed for that. This setting is the explicit app-config knob for a
    # custom / internal CA: a PEM file path OR inline PEM text (the latter so a secrets
    # manager can inject the cert as a value). It's ADDED to the system trust store
    # (public provider CAs still verify). See alkera_core.http.async_client.
    outbound_ca_bundle: str | None = None

    # --- Fetches of a URL somebody else supplied (alkera_core.egress) ---
    # `web.fetch`, OIDC discovery and the like refuse any host that is — or resolves
    # to — a loopback, private, link-local or otherwise non-public address. A
    # self-hosted operator whose wiki or IdP lives on a private network names it
    # here: comma-separated IPs, CIDRs or exact hostnames. Default empty (nothing
    # private is reachable). A zero-length prefix and the link-local metadata range
    # are refused at boot rather than honoured.
    egress_private_allowlist: str = ""
    # Whether a data connection a box opens may reach any host the box can: private
    # ranges, the box's own loopback and the networks it sits on. Metadata addresses
    # and the chat sandbox networks stay refused. Unset, it follows an explicit
    # SELF_HOSTED=true only, never the inferred one: a hosted box has no Stripe keys
    # either. A box on a platform machine credential ignores it.
    connections_reach_local_network: bool | None = None
    # Machines an org admin attaches by SSH details (host, port, username, a
    # password or a private key). Unset, it follows SELF_HOSTED: on for a
    # self-hosted install, off for the hosted product.
    ssh_machines_enabled: bool | None = None
    # Whether an attached machine may be on a loopback or private address. Unset,
    # it follows SELF_HOSTED. The link-local metadata range is refused always.
    ssh_machines_allow_private_addresses: bool | None = None
    # How many redirects one guarded fetch follows; every hop is vetted and pinned
    # like the first. At most EGRESS_MAX_REDIRECTS_CEILING.
    egress_max_redirects: int = 5
    # How long the guard waits for a name lookup before refusing the fetch. No
    # HTTP timeout covers this step — the client's clock starts at connect — so
    # without it one unresponsive authoritative server holds a request thread for
    # the OS resolver's own bound. At most EGRESS_DNS_TIMEOUT_CEILING_SECONDS.
    egress_dns_timeout_seconds: float = 5.0
    # Whether a guarded fetch may go through a forward proxy (HTTP_PROXY /
    # HTTPS_PROXY / ALL_PROXY). The proxy makes the connection, so the address
    # this process vetted is not the address that gets dialed and a name can
    # still answer differently for the proxy than it did for us. The URL is
    # still canonicalised and every address still vetted; only the pin is lost.
    # Off by default: a deployment that wants that trade says so, in writing.
    egress_allow_proxy: bool = False

    # --- Compute providers ---
    # Where a provisioned node's machine credential is stored in Secrets
    # Manager; empty = ``alkera/<app_env>/node/``.
    alkera_node_secret_prefix: str = ""
    # The released daemon a node's bootstrap installs, and where releases live
    # (``<base>/cli/v<version>/linux-x64/alkera-linux-x64.tar.gz``). Read only
    # when a distribution installs its daemon from a release host; by default
    # a node installs the bundle this deployment serves (``node_bundle_dir``).
    alkera_node_daemon_version: str = ""
    alkera_node_release_base_url: str = ""
    # The node bundles this deployment built from source (scripts/build-node-bundle.sh):
    # a directory with manifest.json and alkera-<target>.tar.gz. The backend serves
    # them to nodes and the SSH provider uploads them; empty, there is none.
    node_bundle_dir: str = ""
    # The environment's node channel: with no pinned daemon version a node
    # installs the build ``<base>/cli/nodes/<channel>.json`` names, the pointer
    # the environment's deploy writes when it rolls its boxes (the stable
    # release while the channel names none). Empty = the stable release.
    alkera_node_release_channel: str = ""
    # This checkout's Docker project, written by ``ops/scripts/workspace-env.sh``
    # into ``.env.workspace``. A local developer box (the ``localdev`` provider)
    # is labelled with it, and every listing of local boxes filters by it, so
    # two worktrees' backends never see, adopt or reap each other's boxes.
    compose_project_name: str = ""
    # The API URL a node's daemon talks to; empty = ``frontend_base_url``.
    alkera_node_api_url: str = ""
    # The model gateway a node's chats go through; empty = this server's own
    # gateway on the same origin as the node API URL, at ``NODE_GATEWAY_PATH``
    # (the web proxy's /gateway/ location). A node is always told a gateway, so
    # it never falls back to a default baked into its binary.
    alkera_node_gateway_url: str = ""
    # The sandbox mode a provisioned node runs chats under. Two states only:
    # "gvisor" (the agent runs under gVisor/runsc — the real boundary the shared
    # multi-tenant pool requires; EC2, CPU) or "none" (no boundary — allowed only
    # on a single-tenant node: an enterprise dedicated box, or a dev/RunPod box).
    # Default "gvisor" for a pool-capable deployment; a dev/RunPod stack sets
    # "none". The server keeps a "none" node off the shared pool.
    alkera_node_sandbox_required: Literal["gvisor", "none"] = "gvisor"
    # How much of a pool box one chat may use, by the plan its org is on:
    # ``(vcpu, memory_mb)`` per tier, resolved into the chat row the box reads
    # (``alkera_core.sandbox_tiers``). Enterprise carries no figure here — its
    # bound is the org's own override or its dedicated box's leak guard — and a
    # tier this table does not know fails closed to the free figures.
    sandbox_tier_free_vcpu: int = Field(default=1, ge=1, le=64)
    sandbox_tier_free_memory_mb: int = Field(default=2048, ge=512, le=262144)
    sandbox_tier_plus_vcpu: int = Field(default=2, ge=1, le=64)
    sandbox_tier_plus_memory_mb: int = Field(default=4096, ge=512, le=262144)
    sandbox_tier_pro_vcpu: int = Field(default=4, ge=1, le=64)
    sandbox_tier_pro_memory_mb: int = Field(default=8192, ge=512, le=262144)

    @property
    def sandbox_tier_table(self) -> dict[str, tuple[int, int]]:
        """The tier → ``(vcpu, memory_mb)`` table this deployment runs with."""
        return {
            "free": (self.sandbox_tier_free_vcpu, self.sandbox_tier_free_memory_mb),
            "plus": (self.sandbox_tier_plus_vcpu, self.sandbox_tier_plus_memory_mb),
            "pro": (self.sandbox_tier_pro_vcpu, self.sandbox_tier_pro_memory_mb),
        }

    @property
    def node_secret_prefix(self) -> str:
        return self.alkera_node_secret_prefix or f"alkera/{self.app_env}/node/"

    @property
    def node_api_url(self) -> str:
        return self.alkera_node_api_url or self.frontend_base_url

    @property
    def node_gateway_url(self) -> str:
        if self.alkera_node_gateway_url:
            return self.alkera_node_gateway_url
        return self.node_api_url.rstrip("/") + NODE_GATEWAY_PATH

    # Whether a worker serving the compute meter must hold a provider key.
    # Unset = required on every real deployment and optional in local dev, so a
    # laptop with no key still runs the whole worker while a mis-deployed
    # production worker fails loudly instead of metering nothing in silence.
    # Set false on a deployment that runs no compute plane at all.
    compute_require_provider_key: bool | None = None
    # Optional GLOBAL fallback ceiling (minutes) on a single session lease. None
    # (the default) = no fleet-wide time cap: a session machine runs until its
    # credits are exhausted or it is released, unless the allocation set its own
    # ``max_lease_minutes`` (which always wins). A workspace machine never has one.
    compute_max_lease_minutes: int | None = None
    # MANDATORY hard ceiling (minutes) for a SESSION pod the minute-biller cannot
    # cut off: one stuck in provisioning, or ready but unbillable (no billing
    # account). Defaults to a real value on purpose so such a pod can never run
    # unbounded at the provider's expense. Set to None to disable.
    compute_unbillable_max_minutes: int | None = 120
    # The two halves of the heartbeat contract (alkera_core.compute.liveness):
    # how often the machine's daemon is expected to stamp its liveness, and how
    # long a machine may go unheard before it reads "unreachable". Overriding
    # one without the other is what makes a healthy box read dead, so both
    # defaults come from the one place that states the ratio.
    compute_heartbeat_interval_seconds: int = HEARTBEAT_INTERVAL_SECONDS
    # A workspace machine whose daemon heartbeated within this many seconds is
    # "ready"; older is "unreachable"; never is "starting".
    compute_heartbeat_ready_seconds: int = READY_WINDOW_SECONDS
    # How long a registered box may be silent before the meter reaps it while
    # NO box on the platform has been heard within the ready window — the
    # moment the platform cannot tell a dead fleet from its own deafness (an
    # API that was down, a proxy answering every box 5xx). Once another box
    # proves heartbeats are landing, the ready window alone judges a silent one.
    compute_heartbeat_reap_ceiling_seconds: int = 600
    # How long a box told to stop keeps waiting for the turns it still holds.
    # A drain has no natural end: a turn may run for hours or days, and a chat
    # whose agent is wedged would never release the box at all. The ceiling is
    # not a limit on the work — it is what makes a deploy finishable by an
    # operator, which is the whole reason a box drains rather than being cut.
    # Six hours: past any ordinary turn, so the ordinary deploy costs nobody
    # their answer, and short enough that an operator can plan around it. A box
    # that reaches it puts what is left to sleep the way the idle sweep does —
    # folder pushed, lease handed back — so the turns it could not finish are
    # picked up by the next box rather than lost.
    compute_drain_ceiling_seconds: int = DRAIN_CEILING_SECONDS
    # How long a chat with nothing running and nobody using it stays awake on
    # its box, in minutes. A box otherwise keeps a chat awake until it needs
    # the room (a new chat with every slot taken, or the chats' memory past
    # COMPUTE_CHAT_MEMORY_PRESSURE_PERCENT), and then sleeps the least recently
    # used idle chat first. A chat with anything running (a turn, a background
    # job, a process it left in its sandbox, an ask a person may still answer)
    # never sleeps on this window. Twenty-four hours: a chat left at the end of
    # a working day is still warm the next morning. Nothing meters a chat's
    # awake time, so this costs a pool box its memory and no customer anything.
    # Served to every box this deployment provisions; production refuses less
    # than an hour.
    compute_chat_idle_minutes: int = Field(default=CHAT_IDLE_MINUTES, ge=1)
    # How full the chats' memory on a box may get, as a percentage of its
    # limit, before the box sleeps its least recently used idle chats to give
    # memory back. Read on the working set (resident memory less reclaimable
    # file cache). 100 leaves it to the kernel's own limit alone.
    compute_chat_memory_pressure_percent: int = Field(
        default=CHAT_MEMORY_PRESSURE_PERCENT, ge=1, le=100
    )
    # Which compute provider a WEB chat is placed on by preference. A browser
    # chat runs on the platform's container service and on nothing else, so the
    # default is "container": placement binds a new chat to the org's live
    # container machine when it has one, and only falls back to the org's other
    # live workspace machine when it does not. Nothing provisions a container
    # yet (alkera_core.compute.container refuses every operation), so the
    # fallback is what every deployment sees today. Set "runpod" on a
    # deployment whose chats are meant to run on the org's own RunPod box even
    # once a container exists.
    compute_web_chat_provider: Literal["container", "runpod"] = "container"
    # The name every pod this deployment creates is prefixed with, and the only
    # pods its reconciler will ever touch. It is what ties a pod at the provider
    # back to the allocation that paid for it: the plane names a pod
    # "<prefix>-<allocation id>" and commits that name BEFORE asking the
    # provider, so a create whose answer never arrived is found by name instead
    # of being bought a second time. Two deployments sharing one provider
    # account MUST set different prefixes — a staging reconciler that saw
    # production's pods as orphans would terminate them.
    compute_pod_name_prefix: str = "alkera"
    # How often the reconcile runs: the pass that compares this deployment's
    # pods at each provider with the rows that own them, and that moves every
    # provisioned machine one lifecycle edge (booted, lost, released). A minute,
    # the meter's own cadence: it is what bounds how long the console shows a
    # machine that is gone as still there, and how long a lost box's chats wait
    # to be offered elsewhere. The cost is one listing per configured provider
    # and one describe per live provisioned machine each minute — the same
    # order as the meter's own per-machine status reads, far inside RunPod's
    # and EC2's API rate limits. What it may DESTROY is not sped up by this:
    # the grace and the unconfirmed-create window below are wall-clock ages,
    # so a shorter period reaps nothing sooner.
    compute_reconcile_period_seconds: int = 60
    # The shared secret a provider's shutdown notice is signed with (see
    # alkera_core.compute.shutdown_notice). Unset turns the notice route off —
    # it answers 404 rather than accept a drain it cannot verify. At least 32
    # characters when set.
    compute_shutdown_notice_secret: SecretStr | None = None
    # How old a pod must be before the reconciler is allowed to conclude that
    # nothing owns it. A create in flight has already committed its row, so the
    # grace is not needed for the ordinary race — it is the margin for a
    # provider whose list lags its create, and for a deploy mid-roll. Ten
    # minutes, and a pod whose age the provider does not report is never
    # reaped at all.
    compute_reconcile_grace_seconds: int = 600
    # The ceiling a COMPLETE pod listing is expected to stay under. It is not a
    # page size and not a budget: it is how the pass tells "this is the whole
    # fleet" from "this may be the first page". A listing that reaches it is
    # treated as untrustworthy for the write-off, which is the only step that
    # argues from a pod's ABSENCE, because a row whose machine is running but
    # fell off the end of a truncated list would otherwise be closed and its
    # box then reaped as an orphan on the next pass.
    compute_reconcile_max_pods: int = 5000
    # How many orphaned pods ONE pass may terminate. The destructive action is
    # the one worth budgeting: a pass that suddenly finds hundreds of unowned
    # machines is likelier to be misconfigured (a changed prefix, a restored
    # database) than to have found hundreds of real leaks, and the next tick
    # picks up whatever this one left. Reaching it is logged as an error.
    compute_reconcile_max_terminations: int = 25
    # How long a row may sit in ``provisioning`` with no pod id — a create whose
    # answer never came back — before the reconciler, having found no pod of
    # that name, writes it off. It holds a grant slot until then, so it cannot
    # be left open for ever; it must outlast the grace, or a row would be
    # written off in the same pass that was still forbidden to look for its pod.
    compute_unconfirmed_create_seconds: int = 900
    # How long one refused start (org x machine type x reason) stays on record
    # before an identical refusal is recorded again. A refusal is an ANSWER only
    # a human changes — no grant, no credit, the ceiling in use — and a box that
    # keeps asking would otherwise write a refusal frame and an audit row every
    # retry, burying the trail it is supposed to make legible. 0 disables the
    # dedupe and records every refusal.
    compute_refusal_record_window_seconds: int = 600

    # -- machines an org holds ---------------------------------------------
    # How many minutes of a machine's rate its funding must cover before it is
    # started or woken.
    machine_start_runway_minutes: int = Field(default=60, ge=0)
    # How long a machine whose funding ran out keeps finishing its turns before
    # it is stopped.
    machine_credit_drain_minutes: int = Field(default=5, ge=0)
    # Warn the managers when the funding covers fewer hours than this.
    machine_credit_low_hours: int = Field(default=24, ge=0)
    # Warn again, urgently, when it covers fewer minutes than this.
    machine_credit_urgent_minutes: int = Field(default=60, ge=0)
    # How many days a machine stopped for its funding keeps its disk before the
    # disk is deleted. At least a day: the notices need time to be read.
    machine_unfunded_retention_days: int = Field(default=14, ge=1)
    # How many machines an org may buy on each plan. Free may buy one: machine
    # spend draws on the same usage allowance as tokens. Enterprise is 0 unless
    # a compute grant admits more, in which case its ceiling.
    machine_quota_free: int = Field(default=1, ge=0)
    machine_quota_plus: int = Field(default=2, ge=0)
    machine_quota_pro: int = Field(default=5, ge=0)
    machine_quota_enterprise: int = Field(default=0, ge=0)
    # How long a start the provider had no hardware for keeps being retried,
    # and how often.
    machine_capacity_retry_minutes: int = Field(default=30, ge=0)
    machine_capacity_retry_every_seconds: int = Field(default=300, ge=0)
    # How many times a machine that failed to boot is replaced by a fresh one.
    machine_boot_retry_count: int = Field(default=1, ge=0)
    # How long an org machine may stay silent, or report that it cannot run
    # chats, before it is stopped. Those minutes are not billed, but the
    # provider bills us for them.
    machine_unservable_stop_minutes: int = Field(default=30, ge=1)
    # How long a workspace move waits for running turns to finish before it
    # ends them, and how long it waits for the target machine to wake.
    move_turn_grace_seconds: int = Field(default=120, ge=0)
    # Five minutes: the departed box's hand-back fence (60 s), the lease grant
    # delay (60 s) and a cold take, with room. The lifecycle registry refuses a
    # value that does not exceed the deadlines it waits on.
    move_wake_timeout_seconds: int = Field(default=300, ge=0)
    # When a status stops saying work is happening. A worker renews a running
    # turn's stamp every 15 seconds; a stamp older than the first figure reads
    # "stalled". A machine that answers and has not started a sent message (or
    # taken a chat somebody asked it to wake) within the second reads
    # "stalled" too; it is longer than a cold workspace take, which waits out
    # the file lease grant delay. A box that holds a workspace's folder and
    # has not beaten its lease for the third reads "sync paused".
    chat_turn_silence_seconds: int = Field(default=120, ge=30)
    chat_turn_start_seconds: int = Field(default=180, ge=30)
    files_sync_pause_seconds: int = Field(default=60, ge=20)
    # When the server ends what nothing else will. A running turn whose worker
    # has not renewed its stamp for the first figure is ended with the reason
    # "turn_lost" (it reads stalled long before that); a chat a machine that
    # answers has not taken for the second is put back to sleep with
    # "not_taken" and the message left for the person to send again.
    chat_turn_abandon_seconds: int = Field(default=600, ge=60)
    chat_wake_deadline_seconds: int = Field(default=300, ge=60)
    # A chat whose machine has its message but no free slot for this long is
    # put back to sleep with "no_slot", the message marked never run.
    chat_queue_deadline_seconds: int = Field(default=1800, ge=300)
    # How long a promoted result may wait for the machine to deliver its payload
    # before the cloud calls it failed. A promote creates the object first and
    # asks the machine for the rows second, so "saving…" is a state a reader
    # meets legitimately — but only for as long as there is a machine answering.
    # A box that died between the two never says no, and without a deadline the
    # object reads "Saving…" for ever, with no rows, no reason and no way back.
    # A machine that is alive and cannot find the result refuses the promote
    # outright, so this covers only the absent one.
    objects_promote_deadline_seconds: int = 300

    # --- Chat, machines and connections: bounds a deployment may tune ---
    # Every figure below used to be a module constant nobody could move without a
    # release. They are gathered here so an operator can widen the ones that
    # refuse a person's work and tighten the ones that cost outbound calls.
    # How many Files nodes one chat message may name. A message is a relay, not a
    # transfer, and each named node is one authorization decision on the request.
    # None = no ceiling beyond the one the schema publishes.
    chat_message_max_attachments: int | None = 20
    # How many Files nodes one chat may hold over its whole life — the links it
    # accumulates, not the ones any single message names. Unset by default,
    # because the files a conversation gathers ARE the conversation and refusing
    # the next one is refusing to keep talking; a deployment that would rather
    # bound the table sets a number and gets the refusal back.
    chat_max_attachments: int | None = None
    # How many choices one prompt of an answer to an agent's ask may carry. The
    # options were minted by the harness, so this bounds a forged relay rather
    # than anything a real client sends.
    chat_answer_max_choices: int = 32
    # How long a machine's reason for refusing to publish a chat may be. Over it
    # the whole refusal is rejected and the banner says nothing, so a deployment
    # whose start failures are wordy wants this higher rather than lower.
    chat_refusal_reason_max_chars: int = 500
    # How far below a backward transcript page the read reaches for the prompt
    # row the page's first turn began at, in multiples of the page's own limit.
    # A turn longer than that is cut and the page says so, rather than the
    # read pulling a whole ten-thousand-event turn for one page of it.
    chat_page_turn_reach: int = 4
    # How far below a backward transcript page the read reaches to complete a
    # message the page would otherwise open in the middle of, in multiples of
    # the page's own limit. A prompt the reader sent while the machine was
    # still writing lands INSIDE that answer's rows, so a page anchored on the
    # prompt would carry the answer's last parts without the message they hang
    # on. A message longer than this is not pulled whole for one page of it:
    # the page stops here and says it is cut, and the page below carries the
    # rest. Together with chat_page_turn_reach this sets the ceiling on ONE
    # page: limit + limit * (turn reach + message reach) rows, which is 1400
    # at the 200-row limit a reader opens a chat on.
    chat_page_message_reach: int = 2
    # The largest JSON payload one event-log row may carry. A chat message
    # travels to the other people in the chat as one of these rows, so this is
    # what a long paste is ultimately refused by. The table repeats the absolute
    # ceiling as a CHECK, so this only ever tightens below it.
    event_outbox_payload_max_bytes: int = 2 * 1024 * 1024
    # Whether an org admin may open a chat of their org that nobody shared with
    # them. Off: a private chat is the same opaque not-found to an admin as to
    # any other member, through every door onto it. On: an admin reads it, and
    # reads it only — renaming still takes a rung on the chat, and the
    # transcript's other doors answer the same way this one does.
    chat_org_admin_reads_private: bool = False
    # Whether a workspace may hold more than one chat. With it off every new
    # chat gets a workspace of its own (adopting the chat's folder), and a
    # request to start a chat in an existing workspace is refused. On, a chat
    # with no place named lands in its owner's main workspace, and a chat may
    # be started in any native workspace the caller may write; a box that says
    # it runs workspaces serves all of a workspace's chats over one shared
    # tree, and placement refuses such a chat on a box that does not.
    workspaces_multi_chat: bool = False
    # Whether existing chats are put into workspaces: a chat created without a
    # workspace gets a workspace of one, a read of a chat that names none heals
    # it, and the periodic reconcile pass adopts every chat nobody opens. Off,
    # no workspace row is made for a chat and a chat with no workspace is served
    # as it is. A deploy that brings workspaces in keeps it off until no task of
    # the previous build is serving (that build cannot list a workspace row),
    # and a rollback of the workspaces revision turns it off first.
    workspaces_adoption_enabled: bool = True
    # How many live project workspaces one person may own in one org: an
    # abuse limit, not a plan feature. Main workspaces and the workspace of one
    # a chat is given do not count, and ended ones stop counting the moment
    # they end. Platform staff may raise or lower it for one org
    # (``org_settings.workspace_project_cap``), which wins over this default.
    workspaces_max_projects_per_member: int = Field(default=200, ge=1)
    # How many rows one page of an objects, chats or chat-templates listing may
    # read while looking for the rows the caller may see. A listing cuts every
    # row by the caller's own read answer BEFORE it pages, so a member with few
    # readable rows in an org with many private ones is read past the hidden
    # ones; this bounds that read. A page that reaches it ends the listing
    # early (logged) rather than hand out a cursor into rows the caller may
    # not read. Far above any page size the listings accept.
    # Bounded both ways: zero or less would end every listing on its first page,
    # and a ceiling far past the page sizes turns one hidden-heavy listing into an
    # unbounded table scan per request. A million rows is already minutes of
    # reading for a member who can see none of them.
    objects_list_scan_ceiling: int = Field(default=50_000, ge=1, le=1_000_000)
    # Whether the backend checks, at boot and from /health/ready until it
    # passes, that tenant isolation can be trusted on its database: the Files
    # tenant role is neither superuser nor BYPASSRLS, the login may assume it,
    # every tenant table has row security enabled AND forced with its policy,
    # and a tenant-role read with no org set sees nothing. Unset means on for
    # staging and production, off for local (a laptop's dev database is the
    # suite's superuser and would pass anyway). A failing check takes the task
    # out of rotation (503 "row-level security canary failed") and logs why.
    row_security_canary: bool | None = None
    # How long chat creation waits on the gateway's model catalog before calling
    # it an outage. A slow gateway fails the creation itself, so a deployment
    # whose gateway is a region away wants this higher.
    chat_model_catalog_timeout_seconds: float = 10.0

    # --- Secrets at rest (encryption of persisted secrets, e.g. SSO IdP client secret) ---
    # By default the encryption key is DERIVED from AUTH_JWT_SECRET (no extra key to
    # manage). A security-conscious deployment can hold its OWN key (BYOK): a
    # urlsafe-base64 32-byte Fernet key. To rotate, set the new key here and move the
    # old one(s) into SECRET_BOX_KEYS_PREVIOUS (comma-separated) — existing ciphertext
    # still decrypts and re-encrypts on its next write.
    secret_box_key: str | None = None
    secret_box_keys_previous: str = ""

    # --- Gateway upstream mode (self-hosted) ---
    # A self-hosted gateway reaches models one of two ways:
    #   "direct" (default) — call the providers itself with the customer's keys /
    #     Bedrock IAM. Nothing leaves the VPC except the provider call.
    #   "proxy" — forward to the upstream gateway at ALKERA_PROXY_URL (for
    #     example a hosted gateway) with an
    #     org-scoped proxy token. The upstream holds the provider keys and meters
    #     the AGGREGATE usage per token (no per-user identity is sent). In this
    #     mode the self-hosted gateway needs NO provider keys.
    # The gateway still authenticates the local user + meters them against the
    # local (Layer-2) ledger BEFORE forwarding — only the upstream call changes.
    gateway_upstream: str = "direct"
    # The upstream gateway's base URL for proxy mode and the self-hosted
    # heartbeat. No default: a self-hosted gateway serves its own users with its
    # own provider keys unless an operator names an upstream here.
    alkera_proxy_url: str = ""
    # The org-scoped proxy token minted by the upstream deployment. Required in proxy mode.
    alkera_proxy_token: str | None = None
    # A request may settle slightly past its reservation (actual usage can exceed
    # the input+output hold via cache/reasoning tokens). We allow a balance to go
    # this far negative, then clamp — bounding the worst-case overspend per
    # request. Default $0.50.
    billing_max_overdraft_nanos: int = 500_000_000
    # Optional path to a normalized provider-cost reference JSON (models.dev /
    # litellm, ingested into our shape) for the reconciliation drift report.
    # Unset → reconciliation only does negative-margin detection.
    billing_reference_prices_path: str | None = None
    # Reservation liveness. A gateway holding a stream stamps its ProxyRequest
    # row every `touch` seconds, and the sweeper reclaims an in-flight hold whose
    # row has gone `stale` seconds without a stamp — so a reservation orphaned by
    # a SIGKILL is released in minutes instead of waiting out
    # GATEWAY_MAX_STREAM_SECONDS, which a genuinely live step is entitled to use
    # in full. The stale window must survive a missed stamp (a slow DB, a paused
    # event loop), so it has to exceed two touch intervals — production refuses a
    # narrower pair, because reclaiming a live stream's hold makes its settle a
    # no-op and the usage is served free.
    billing_reservation_touch_seconds: float = 60.0
    billing_reservation_stale_seconds: int = 900
    # Postpaid ("additional") pools are never funded — Alkera invoices the metered
    # records — so their balance carries a standing headroom grant purely so a
    # request can reserve against it. This is that guardrail, in USD. It is not a
    # bill and not a spend limit (the org's per-period cap is), so it only has to
    # be comfortably above a single request's estimated cost: a buffer under that
    # 402s a postpaid org for requests it is entitled to make.
    billing_postpaid_reserve_buffer_usd: Decimal = Field(default=Decimal("10"), gt=0)

    # Explicit deployment-shape override (env SELF_HOSTED). None = infer from Stripe
    # config (Alkera's SaaS always has Stripe keys; a customer self-host never does).
    # Self-hosted deploys SHOULD set SELF_HOSTED=true so the signal stays correct even
    # for a component that legitimately lacks Stripe keys (e.g. the gateway). Drives
    # `is_self_hosted` — the ENTERPRISE plan, the hidden proxy-tokens page, and the
    # hard "never phone telemetry home" gate (see observability/sentry.py).
    self_hosted: bool | None = None

    # --- Gateway bounds a deployment may tune ---
    # Figures the gateway used to hold as module literals in `model_gateway`.
    # None of them bounds a turn (see the per-step section above for those); each
    # is a knob an operator can reasonably want to move — a provider's retry
    # behaviour, how large a prompt may be, how much of an upstream error a user
    # is shown. Every one defaults to the value that was hard-coded, so folding
    # them here changed no behaviour; they are read at call time, so a test (and
    # a restart) can move them.
    #
    # Attempts to OPEN an upstream stream on one route before the gateway fails
    # over to the next candidate route. A single-route model has no next
    # candidate, so on that model this is the whole tolerance for a provider blip.
    gateway_upstream_max_attempts: int = 3
    # Longest provider-requested pause (`Retry-After`) the gateway will honor in
    # line. A hint above this skips the retry entirely and fails over instead: an
    # admitted request pins a gate slot, a credit reservation and a DB row for the
    # whole wait, so waiting longer than this costs more than trying elsewhere.
    gateway_retry_after_cap_seconds: float = 30.0
    # The exponential-backoff curve between those attempts: wait
    # `base * 2^(n-1)`, clamped to `max`, plus a uniform draw from
    # `[0, jitter]`. The jitter is what stops a provider blip that refused a
    # thousand requests at once from having all thousand return together.
    gateway_retry_backoff_base_seconds: float = 0.5
    gateway_retry_backoff_max_seconds: float = 8.0
    gateway_retry_backoff_jitter_seconds: float = 0.25
    # The three legs of the upstream httpx timeout that are NOT about how long a
    # model may think (the read leg is `gateway_upstream_read_timeout_seconds`
    # above): opening the TCP/TLS connection, writing the request body, and
    # waiting for a free connection in the shared pool.
    gateway_upstream_connect_timeout_seconds: float = 10.0
    gateway_upstream_write_timeout_seconds: float = 30.0
    gateway_upstream_pool_timeout_seconds: float = 10.0
    # Where tiktoken looks for the BPE ranks the token estimator counts with.
    # tiktoken fetches them over the network the first time an encoding is used,
    # synchronously — and the estimator is on the admission path, so on an
    # egress-restricted install that fetch would block the event loop for a TCP
    # connect timeout on the first OpenAI-family request after every start. The
    # gateway image bakes the encodings and points this at them; a source
    # checkout points it at the repo's ignored cache so a developer downloads
    # them once. Empty leaves TIKTOKEN_CACHE_DIR alone (tiktoken's own default).
    gateway_tokenizer_cache_dir: str = ""
    # Deployment kill switch for the agent's `web.fetch` tool. It is a READ tool
    # in every permission mode, so a read-only cloud chat can pull any public URL
    # the model names, and the only thing in front of it is the network floor. Off
    # here and the gateway stops advertising it, so the tool is never registered
    # on any box this deployment serves — an env change and a restart, not an
    # image roll or a VPC rule. It only ever subtracts from the org's own
    # web-tools toggle; it cannot turn the tool on for an org that disabled it.
    agent_web_fetch_enabled: bool = True
    # How long the process waits at STARTUP for the encodings to load before it
    # serves without them. The load runs once, off the request path; a load that
    # is merely slow is still adopted when it lands, and one that never lands
    # costs the estimator nothing but its calibrated chars-per-token ratio.
    gateway_tokenizer_warm_timeout_seconds: float = 10.0
    # Largest request body the gateway accepts (bytes), prompt plus attachments.
    # The body is buffered whole before any credit is reserved, so this is what
    # stops concurrent uploads from OOMing every other tenant's in-flight stream.
    gateway_max_request_body_bytes: int = 32 * 1024 * 1024
    # Longest client-supplied idempotency key accepted. It is persisted as the
    # ProxyRequest's `request_id`, a VARCHAR(255) — a longer value would fail in
    # the database mid-admission, so the ceiling refuses anything above that.
    gateway_max_idempotency_key_length: int = 200
    # The share of a process's stream slots one principal may hold at once: the
    # per-principal cap is `gateway_max_concurrent_streams // this`, at least one.
    # Without a per-principal bound one account can open enough streams to shed
    # every other tenant with a 429. A larger number is a stricter share.
    gateway_per_principal_stream_share: int = 4
    # The `Retry-After` on a request the gateway itself refuses without reaching
    # a provider — shed for capacity, or refused because the process is draining.
    # Slots free continuously as streams end and a replacement task is already
    # coming up, so "shortly" genuinely means seconds; the value exists so a
    # proxy-mode caller (which honors this gateway's pacing exactly as it honors
    # a provider's) gets a real hint instead of blind backoff.
    gateway_shed_retry_after_seconds: int = 1
    # How long before the reservation sweeper's cutoff a live stream is cut, so
    # its settle lands while the hold is still claimable. Below the cut the
    # pipeline keeps a one-second floor, so a deployment cannot misconfigure this
    # into a zero-length stream.
    gateway_stream_settle_margin_seconds: float = 60.0
    # The watchdog that tears down a stream which outlived the cap above closes
    # the upstream BEFORE it frees the capacity slot, and a relay parked mid-step
    # cannot be closed from outside. So it retries the close every
    # `retry_seconds` for at most `close_seconds`, then frees the slot anyway
    # rather than pin capacity on a relay wedged inside an await.
    gateway_stream_expiry_close_seconds: float = 10.0
    gateway_stream_expiry_retry_seconds: float = 0.25
    # How much of a non-JSON upstream error body is shown to the caller
    # (characters). Provider bodies can be whole HTML pages, and the text reaches
    # a user's terminal and our logs.
    gateway_upstream_error_excerpt_chars: int = 300

    # --- Entitlements (signed, offline-verified feature grants) ---
    # See alkera_core.entitlements. The customer-set token env var is deliberately
    # NOT documented in .env.example or the deploy docs — Alkera communicates it 1:1.
    # A malformed value must never crash a customer boot (fail closed and quiet), so
    # no validator ever inspects it.
    alkera_entitlements: str | None = None
    # TEST-ONLY seam for the token-verification public key. Honored ONLY when
    # running under pytest (see alkera_core.entitlements._dev_override_honored) —
    # a running backend/gateway/worker ignores it, so a customer can't repoint
    # verification at their own key by flipping APP_ENV. Never a customer knob.
    alkera_entitlements_public_key: str | None = None
    # The Ed25519 signing seed (base64url raw 32 bytes), set only on the backend
    # that issues entitlements. Its presence enables the platform-admin minting surface.
    alkera_entitlements_signing_key: str | None = None

    # --- Billing / Stripe ---
    # Stripe handles SUBSCRIPTION STATE + PAYMENT COLLECTION only; our ledger stays
    # the source of truth for credits/usage. Keys live in gitignored .env.local
    # locally (test mode) and in the deployment's secret store otherwise. With none set the app
    # is Free-only: the paid Checkout/Portal routes + the webhook return 503 and the
    # paid plan buttons stay hidden (see `stripe_configured`).
    stripe_secret_key: str | None = None
    stripe_publishable_key: str | None = None
    # Webhook signing secret. Locally `stripe listen` prints it; in prod it's the
    # dashboard endpoint's whsec_. Required (with the secret key) to provision.
    stripe_webhook_secret: str | None = None
    # Per-(tier, interval) recurring Price IDs from the Stripe dashboard. The
    # billing extension's tier enum maps tier+interval -> the attr name here;
    # Free has no price. Adding ANNUAL is two more attrs + enum data, no migration.
    stripe_price_plus_monthly: str | None = None
    stripe_price_pro_monthly: str | None = None
    # Optional Customer Portal configuration id (manage/cancel/downgrade screens).
    stripe_portal_configuration_id: str | None = None
    # Pinned Stripe API version sent on every client call. None -> the SDK's
    # bundled default for the installed stripe version (a safe pin in itself).
    stripe_api_version: str | None = None

    # --- Org usage analytics ---
    # The org-admin cost/usage dashboard (`GET /api/v1/org/usage`). Off -> the
    # route answers 404, so the surface looks nonexistent rather than locked.
    # ORG_USAGE_METRICS narrows WHICH sections the endpoint returns: some
    # granularity (per-member spend, the per-request feed) can be more than an
    # org wants disclosed, so the operator picks. Comma-separated keys from
    # ORG_USAGE_METRIC_KEYS, or "all". An unknown key refuses to boot -- a typo
    # would otherwise silently hide a section.
    org_usage_analytics_enabled: bool = True
    org_usage_metrics: str = "all"

    # --- Files (the object storage layer) ---
    # Master switch: the Files routes mount only when this is true, so day one
    # ships dark and a deployment opts in once its store and content domain are
    # wired. Everything below is inert (and unvalidated) while it is false.
    files_enabled: bool = False
    # Which store driver serves object bytes. `filesystem` is a local/self-hosted
    # volume; `s3_compatible` is any S3 API endpoint (SeaweedFS locally, B2, a
    # customer's bucket); `aws` is S3 proper plus STS session-tag vending.
    files_store_provider: Literal["filesystem", "s3_compatible", "aws"] = "filesystem"
    # Root directory for the `filesystem` driver. Unset resolves lazily to
    # `<project root>/.alkera-files` (see `files_store_root_path`) — never
    # `Path.home()`, which is not a deployment-owned location.
    files_store_root: Path | None = None
    files_store_endpoint: str | None = None
    files_store_bucket: str | None = None
    files_store_region: str = "us-east-1"
    # Static credentials for the S3-compatible driver (dev and self-hosted). On
    # AWS the task role is used instead and BOTH stay empty; a half-set pair is a
    # misconfiguration the production validator refuses.
    files_store_access_key: str | None = None
    files_store_secret_key: SecretStr | None = None
    # `path` (endpoint/bucket/key) works on every S3-compatible endpoint;
    # `virtual` (bucket.endpoint/key) is what AWS prefers.
    files_store_addressing: Literal["path", "virtual"] = "path"
    # `proxied` streams bytes through the API; `direct` hands the client a
    # presigned URL and needs a driver that can sign one.
    files_transfer_mode: Literal["proxied", "direct"] = "proxied"
    # Origin of the separate content domain user bytes are served from, e.g.
    # `https://c.alkerausercontent.com` or `http://files.localhost:8000` locally.
    # It must never share a hostname with the API or the SPA: cookies ignore
    # ports, so a shared hostname would put the session cookie on user content.
    files_content_base_url: str | None = None
    # HMAC key the short-lived content tokens are signed with. Local falls back
    # to a known dev value like the other dev secrets (`effective_files_content_signing_key`).
    files_content_signing_key: SecretStr | None = None
    # Bytes below which a version's content is stored inline with the row rather
    # than as a store object.
    files_inline_max_bytes: int = 65536
    # How long a request may spend stamping a brand-new dedup domain's prefix
    # with this deployment's ownership marker. Signing up and making an org are
    # what pay it, and neither has any use for the answer -- only the `files.gc`
    # collector reads the marker -- so the write gets a deadline short enough
    # that an unreachable or wedged store is a rounding error on the request
    # rather than its whole latency. Past the deadline the domain is left
    # unmarked (the safe state: the collector reports it and never collects it)
    # and the janitor's marker sweeper writes it later.
    files_store_stamp_timeout_seconds: float = 0.5

    # --- Files: bounds a deployment may tune ---------------------------------
    # Figures the subsystem used to spell as module constants. Each default is
    # the value the code shipped with, and each is read through `get_settings()`
    # where it is used rather than frozen at import, so a deployment (or a test)
    # can move the bound without touching the module that enforces it.
    #
    # How long a signed content URL stays redeemable: a single file download
    # (`files_content_url_ttl_seconds`), a page grant covering a rendered
    # folder (`files_page_grant_ttl_seconds`), and the archive link a "download
    # as zip" operation hands back (`files_archive_url_ttl_seconds`). Long
    # enough for a browser to start the transfer, short enough that a URL
    # pasted somewhere public is worthless by the time it is read.
    files_content_url_ttl_seconds: int = 300
    files_page_grant_ttl_seconds: int = 900
    files_archive_url_ttl_seconds: int = 900
    # How a "download as zip" reads the subtree it archives. The walk is a
    # keyset scan, so `files_archive_page_nodes` is at once the largest number
    # of node rows resident at any moment and the batch the access decision is
    # made over -- a constant number of statements per page, whatever the
    # subtree's size. `files_archive_max_recorded_skips` caps what the
    # operation's `errors[]` and the archive's `_alkera/skipped.txt` enumerate;
    # past it both state how many more were omitted rather than growing one
    # unbounded document. `files_download_inline_max_nodes` is the size at
    # which the request stops enumerating the plan for the caller: below it the
    # walk runs in the request so `errors[]` names every omission up front,
    # above it the operation carries the subtree's size and the omissions are
    # reported by the archive itself when it is redeemed, which is the only
    # place they can be decided fresh anyway.
    files_archive_page_nodes: int = 1_000
    files_archive_max_recorded_skips: int = 1_000
    files_download_inline_max_nodes: int = 10_000
    # The most requests one page grant will ever serve. A page and its assets is
    # tens of requests; a ceiling three orders of magnitude above that costs a
    # real reader nothing and stops a grant being used to walk a folder. A
    # deployment serving documents that reference hundreds of assets raises it
    # here rather than living with content that stops loading as bare 404s.
    files_page_grant_max_requests: int = 5_000

    # Upload shape limits: the largest proxied part, and the largest body a
    # single-shot PUT may carry before the client must open a session.
    #
    # --- The upload ceiling, and everything that moves with it ---------------
    #
    # `files_max_file_bytes` is the largest single file this deployment
    # accepts: 1000 GB, decimal, because that is the unit the product states
    # (`alkera_core.units` -- a GB is 1,000,000,000 bytes everywhere storage is
    # read or typed). `files_max_upload_parts` is the most parts one session may
    # be cut into. Both ride in the `limits` object on the `POST /uploads`
    # response -- the only place they are published; there is no separate
    # discovery route -- and both are refused when the session is opened, so a
    # client learns the ceiling before it spends a transfer rather than at the
    # commit.
    #
    # None of these three is a number that can be raised on its own. A part is
    # PROXIED -- the client PUTs it to
    # `/api/v1/files/uploads/<session>/parts/<n>`, nothing is signed straight to
    # the object store -- so every hop between a browser and the store carries
    # one part-sized body, and the file ceiling a caller really meets is
    # `files_part_max_bytes * files_max_upload_parts`, not the figure below.
    # `_validate_files_upload_ceiling` refuses a pair that would put the parts
    # limit in front of the file limit, so the published number is always the
    # one a user actually hits. Raising the ceiling means moving ALL of these
    # together:
    #
    #   * `files_part_max_bytes` x `files_max_upload_parts` (here). S3 multipart
    #     accepts at most 10,000 parts of at most 5 GiB, so the part COUNT is
    #     that hard 10,000 -- the driver refuses above it -- and the part SIZE
    #     is what grows. 1000 GB at 128 MiB parts is 7,451 parts; a file this
    #     large needs parts of at least ~100 MB for the count to fit at all.
    #     `alkera_core.files.uploads.PART_SIZE` is this setting, not a second
    #     literal, so what `limits` publishes and what a part PUT enforces
    #     cannot drift apart.
    #   * the pre-buffer Content-Length guard on the upload prefix:
    #     `GUARDED_PREFIXES["/api/v1/files/uploads/"]` in
    #     `apps/backend/backend/api/body_limit.py`, which must admit one part
    #     plus envelope headroom. That table is also the SOURCE of the WAF's
    #     exempt-path JSON -- run `make gen-waf-paths` and commit the artifact.
    #   * the SaaS edge: the WAF body exemption for the files prefixes and the
    #     per-IP budget on them (`waf_files_rate_limit`). One max-size upload is
    #     `files_max_file_bytes / files_part_max_bytes` part PUTs from one
    #     address inside one rolling five-minute window.
    #   * the self-hosted edge: `client_max_body_size` on the files location in
    #     `apps/web/nginx.conf.template`, and the proxy-body-size /
    #     proxy-request-buffering annotations on the files ingress.
    #   * the chat composer's attachment cap. The browser composer uploads
    #     through this same session API, so its cap IS this ceiling. It declares
    #     no cap of its own: the server refuses at `POST /uploads`, before a byte
    #     is sent, and that refusal is the sentence the user reads.
    files_part_max_bytes: int = 128 * (1 << 20)
    files_single_put_max_bytes: int = 67108864
    files_max_file_bytes: int = 1_000_000_000_000
    files_max_upload_parts: int = 10_000
    # The admission budget for streaming parts, in RESIDENT bytes: a part PUT is
    # admitted while ``held_parts x FILES_UPLOAD_PART_RESIDENT_BYTES`` stays
    # under it, and shed with a 503 + Retry-After otherwise. Resident, not wire:
    # a 128 MiB part costs 12 MiB of process memory while it streams, so a
    # budget counted in wire bytes admitted two parts per process for the whole
    # fleet. The arithmetic behind the default is on the two constants above.
    # Fairness rides the same budget: a principal may hold at most its share
    # (``capacity / active principals``, never under one) while others are
    # asking, and the whole budget when nobody else is.
    files_upload_resident_budget_bytes: int = (
        FILES_UPLOAD_DEFAULT_CONCURRENT_PARTS * FILES_UPLOAD_PART_RESIDENT_BYTES
    )
    # Per-org defaults a drive's quota is seeded with (100 GB, one million
    # nodes). Storage is counted in the units it is sold in: 100 GB is
    # 100,000,000,000 bytes, so the figure an operator reads back is the one
    # they set (`alkera_core.units`).
    files_quota_default_bytes: int = 100_000_000_000
    files_quota_default_nodes: int = 1_000_000
    files_max_open_sessions_per_org: int = 1000
    # Folder-lease fencing: how long a grant lives, how often the holder must
    # renew it, and how long a waiter backs off before it may take the lease over.
    files_lease_ttl_seconds: int = 600
    files_lease_heartbeat_seconds: int = 10
    files_lease_grant_delay_seconds: int = 60
    # The live plane a streaming holder runs on. How long a change is coalesced
    # before it is reported, how often a batch goes out, how many entries one
    # batch may name, how large a file may be before it stays on the machine
    # instead of being uploaded, the bandwidth a holder may spend in a minute
    # before it starts postponing, and the ceiling on entries in flight under
    # one lease. The batch ceiling must stay at or below the in-flight ceiling,
    # or a holder at the defaults could never make progress.
    files_live_debounce_ms: int = 300
    files_live_batch_ms: int = 500
    files_live_max_batch_entries: int = 256
    files_live_max_file_bytes: int = 33_554_432
    files_live_bandwidth_bytes_per_minute: int = 134_217_728
    files_live_max_pending_entries: int = 500_000
    # The holder's tree report. How long a change waits to be batched with the
    # ones after it, how many entries one batch may name (the route refuses a
    # longer one with 413), and the body size past which the holder gzips it.
    files_live_metadata_every_ms: int = 300
    files_live_metadata_max_entries: int = 2_000
    files_live_metadata_gzip_bytes: int = 8_192
    # How many files under one lease may carry the holder's report at once. A
    # batch that would pass it is refused whole with `files.live_too_many`; the
    # bytes keep landing, and each landing clears a report.
    files_holder_max_nodes: int = 2_000_000
    # Conflicted copies. When both the web and the machine holding a folder
    # changed one file, the last write to reach the drive keeps the name and
    # the displaced bytes become a conflicted-copy file beside it. At most this
    # many copies sit beside one original; past it a further displacement
    # becomes a new version of the newest copy, so a file both sides keep
    # fighting over cannot fill its folder with copies.
    files_conflict_copies_max: int = 5
    # A machine that stops holding a folder -- released, or gone quiet until
    # its lease lapsed -- leaves rows whose bytes never landed. They read
    # "unsynced" for this long, so the same machine coming back reconciles
    # them; past it the reaper trashes them (files with landed bytes only lose
    # the facet). And how long a holder handing a folder back keeps landing
    # queued bytes before it releases and reports what is left.
    files_unsynced_grace_seconds: int = 86_400
    files_release_drain_seconds: int = 120
    # Bytes on demand. A reader opening a file whose bytes are still only on
    # the machine holding its folder asks that machine for the one file and
    # waits for it to land: this long for a download or a file grant, this
    # long for a page grant (a page also fetches what sits beside it, so it
    # gives up sooner and lets the browser retry), and this long for the
    # machine to answer at all before the wait ends early. A folder's machine
    # is asked at most this many times a minute by one process; past it a
    # reader is told the machine is busy and retries.
    files_promote_wait_seconds: float = 8.0
    files_promote_page_wait_seconds: float = 3.0
    files_promote_ack_seconds: float = 2.0
    files_promote_per_minute: int = 600
    # The role STS session-tag credentials are vended from (the `aws` provider's
    # scoped-handle mechanism; without it a tenant handle cannot be scoped).
    files_vend_role_arn: str | None = None
    # Run a queued Files operation (an upload promote, a copy, an oversized
    # move) inline at the end of the request that queued it, through the same
    # library core the worker family calls. The response is unchanged — still a
    # 202 naming a queued operation — so a client's progress path is the same
    # either way; only *who* runs the work moves. For the single-process
    # self-hosted shape (no Temporal worker) and for the test suite; SaaS keeps
    # the worker, which is why production refuses it.
    files_inline_operations: bool = False
    # How many orgs one janitor activity sweeps before it returns the org it
    # stopped at. A pass is a sequence of these: the workflow hands the cursor
    # back until a page runs short, so an install with thousands of tenants is
    # swept by many bounded activities rather than one that a Temporal timeout
    # kills mid-fleet — which would restart at the first org every tick and
    # leave the tail never swept.
    files_janitor_org_budget: int = 50

    # How many times the recovery sweep offers an abandoned `queued`
    # operation to a runner before it fails the row instead. The hand-off
    # every Files route does is best-effort and bounded (two seconds), so a
    # briefly unreachable orchestrator — or a process killed after it
    # answered 202 — leaves a durable row with nobody named to run it. Three
    # offers, one per sweep tick, outlast a restart; past them the row is
    # failed with a reason, because a client polling `queued 0/N` forever is
    # worse served than one told the work was abandoned. The staleness
    # threshold is the library's QUEUED_STALE_AFTER, shared with the read
    # that re-hands a stray home move.
    files_queued_recovery_attempts: int = 3
    # The most abandoned operations one sweep tick takes, ACROSS the fleet --
    # not per tenant. A bound, not a rate: the sweep exists for a rare lost
    # hand-off, so a backlog past it is taken by the next tick rather than by
    # one pass that runs long, and one number is the whole tick's size rather
    # than a factor of it.
    files_queued_recovery_budget: int = 200
    # Whether the daily `files.gc` pass may run at all. It is the one Files job
    # that erases bytes on the strength of what ONE database can see, so it is
    # opt-in everywhere but a developer's machine: unset means on when
    # APP_ENV=local and off otherwise, and an operator turns it on once the
    # database login is known to bypass row security and a dry run reads clean.
    files_gc_enabled: bool | None = None
    # The share of one page's own dedup domains that may read as orphans before
    # the pass refuses to collect any of them. `--allow-mass-collect` overrides
    # it for a single run.
    files_gc_max_orphan_fraction: float = 0.1
    # The global version rule: how many of the newest versions a node keeps
    # whatever their age, and how long a version is kept whatever the count --
    # whichever of the two keeps MORE. The defaults are the rule Alkera hosts
    # (`alkera_core.files.retention`); a self-hosted deployment that wants
    # deeper history raises them, and a per-file `keep_versions` retention
    # label overrides them for one path. A negative value is a
    # misconfiguration, and the pruner reads it as "keep everything" rather
    # than deleting on it.
    files_version_keep_newest: int = 5
    files_version_keep_window_days: int = 90

    # --- Account lifecycle (account deletion) ----------------------------------
    # How long a scheduled account deletion waits before the erasure runs; the
    # person can cancel at any point inside it.
    account_deletion_grace_days: int = 14
    # How recent a sign-in must be for an account without a password to request
    # its deletion (the password step-up's counterpart for federated accounts).
    account_reauth_window_seconds: int = 600

    @property
    def files_store_root_path(self) -> Path:
        """Root directory the `filesystem` driver writes packs under.

        Resolved lazily so an unset value follows the process's project root
        rather than being frozen at import; never `Path.home()` — a store root
        must be a location the deployment owns, not the invoking user's."""
        if self.files_store_root is not None:
            return self.files_store_root
        return (self.alkera_project_root or Path.cwd()) / ".alkera-files"

    @property
    def files_content_host(self) -> str | None:
        """`hostname[:port]` of the content origin, lowercased. None when unset.

        The content mount refuses any other Host header, so this is the value it
        compares against; the port is kept because local dev distinguishes the
        content origin from the API only by hostname AND serves both on ports."""
        base = self.files_content_base_url
        host = _host_of(base)
        if host is None:
            return None
        assert base is not None
        raw = base.strip()
        port = urlsplit(raw if "//" in raw else f"//{raw}").port
        return f"{host}:{port}" if port is not None else host

    @property
    def files_store_capabilities_hint(self) -> frozenset[str]:
        """What the configured provider can do natively, before the driver is
        constructed. The driver's own record wins at runtime; this is what the
        boot validator checks a settings combination against."""
        return FILES_STORE_CAPABILITIES.get(self.files_store_provider, frozenset())

    @property
    def files_store_driver(self) -> str:
        """The `file_stores.driver` this provider is persisted as.

        The only translation between the provider a deployment configures and
        the driver catalogue the store table admits; every writer reads this
        rather than the provider, so the two vocabularies cannot drift again.
        """
        return FILES_STORE_DRIVERS[self.files_store_provider]

    @property
    def effective_files_content_signing_key(self) -> str:
        """HMAC key for content tokens, with a known dev fallback in `local`
        (also when env supplies an empty string)."""
        key = self.files_content_signing_key
        if key is not None and key.get_secret_value():
            return key.get_secret_value()
        return self._local_fallback(
            "FILES_CONTENT_SIGNING_KEY", _LOCAL_DEV_FILES_CONTENT_SIGNING_KEY
        )

    @property
    def egress_private_allowlist_entries(self) -> list[str]:
        return [e.strip() for e in self.egress_private_allowlist.split(",") if e.strip()]

    @field_validator("egress_private_allowlist")
    @classmethod
    def _valid_egress_private_allowlist(cls, v: str) -> str:
        from alkera_core.egress import parse_allowlist

        try:
            parse_allowlist(v.split(","))
        except ValueError as exc:
            raise ValueError(f"EGRESS_PRIVATE_ALLOWLIST: {exc}") from exc
        return v

    @field_validator("egress_max_redirects")
    @classmethod
    def _valid_egress_max_redirects(cls, v: int) -> int:
        if not 0 <= v <= EGRESS_MAX_REDIRECTS_CEILING:
            raise ValueError(
                f"EGRESS_MAX_REDIRECTS must be between 0 and {EGRESS_MAX_REDIRECTS_CEILING}"
            )
        return v

    @field_validator("egress_dns_timeout_seconds")
    @classmethod
    def _valid_egress_dns_timeout(cls, v: float) -> float:
        if not 0 < v <= EGRESS_DNS_TIMEOUT_CEILING_SECONDS:
            raise ValueError(
                "EGRESS_DNS_TIMEOUT_SECONDS must be greater than 0 and at most "
                f"{EGRESS_DNS_TIMEOUT_CEILING_SECONDS:g}"
            )
        return v

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.api_cors_origins.split(",") if o.strip()]

    @property
    def csrf_trusted_origins_list(self) -> list[str]:
        return [o.strip() for o in self.auth_csrf_trusted_origins.split(",") if o.strip()]

    @field_validator("auth_csrf_trusted_origins")
    @classmethod
    def _origins_only(cls, v: str) -> str:
        """Every entry must be a bare origin. A value carrying a path, a query,
        credentials or a wildcard would silently never match the `Origin` header
        a browser sends, which reads as "the guard is broken" rather than "the
        setting is wrong"."""
        for entry in (o.strip() for o in v.split(",")):
            if not entry:
                continue
            parsed = urlsplit(entry)
            host = parsed.hostname or ""
            if (
                parsed.scheme not in {"http", "https"}
                or not host
                # A wildcard or any other non-host character can never equal the
                # `Origin` a browser sends; ':' admits a bracketless IPv6 literal.
                or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-:" for c in host)
                or parsed.username is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    f"AUTH_CSRF_TRUSTED_ORIGINS entry {entry!r} is not a bare "
                    "origin (expected http(s)://host[:port])"
                )
        return v

    @field_validator("org_usage_metrics")
    @classmethod
    def _known_org_usage_metrics(cls, v: str) -> str:
        keys = {k.strip() for k in v.split(",") if k.strip()}
        unknown = keys - ORG_USAGE_METRIC_KEYS - {"all"}
        if unknown:
            raise ValueError(
                f"Unknown org usage metric(s): {', '.join(sorted(unknown))}. "
                f"Valid: all, {', '.join(sorted(ORG_USAGE_METRIC_KEYS))}"
            )
        return v

    @field_validator("database_pool_size")
    @classmethod
    def _pool_size_sane(cls, v: int) -> int:
        """Zero resident connections would turn every checkout into overflow churn
        (or, with overflow 0 too, an engine that can never connect); an outsized
        pool silently eats the shared instance's connection budget. Refuse both."""
        if not 1 <= v <= 100:
            raise ValueError(
                "DATABASE_POOL_SIZE must be between 1 and 100 (resident connections "
                "per process; size the FLEET against the database instance's "
                "max_connections, not one process)"
            )
        return v

    @field_validator("database_pool_max_overflow")
    @classmethod
    def _pool_overflow_sane(cls, v: int) -> int:
        if not 0 <= v <= 100:
            raise ValueError(
                "DATABASE_POOL_MAX_OVERFLOW must be between 0 and 100 (burst "
                "connections per process on top of DATABASE_POOL_SIZE)"
            )
        return v

    @field_validator("database_lock_timeout_ms", "database_background_lock_timeout_ms")
    @classmethod
    def _lock_timeout_sane(cls, v: int) -> int:
        """Off, or long enough that ordinary contention between two short
        writers clears, and short enough that it still bounds anything."""
        if v != 0 and not 100 <= v <= 600_000:
            raise ValueError(
                "a database lock timeout must be 0 (off) or between 100 and 600000 milliseconds"
            )
        return v

    @field_validator("database_statement_timeout_ms", "database_background_statement_timeout_ms")
    @classmethod
    def _statement_timeout_sane(cls, v: int) -> int:
        if v != 0 and not 1_000 <= v <= 3_600_000:
            raise ValueError(
                "a database statement timeout must be 0 (off) or between 1000 and "
                "3600000 milliseconds"
            )
        return v

    @field_validator(
        "database_idle_in_transaction_timeout_ms",
        "database_background_idle_in_transaction_timeout_ms",
    )
    @classmethod
    def _idle_in_transaction_timeout_sane(cls, v: int) -> int:
        if v != 0 and not 10_000 <= v <= 86_400_000:
            raise ValueError(
                "a database idle-in-transaction timeout must be 0 (off) or between 10000 and "
                "86400000 milliseconds"
            )
        return v

    @field_validator("database_pool_pressure_log_interval_seconds")
    @classmethod
    def _pool_pressure_interval_sane(cls, v: float) -> float:
        if not 1.0 <= v <= 3600.0:
            raise ValueError(
                "DATABASE_POOL_PRESSURE_LOG_INTERVAL_SECONDS must be between 1 and 3600"
            )
        return v

    @field_validator("gateway_max_concurrent_streams")
    @classmethod
    def _stream_cap_within_pool(cls, v: int) -> int:
        """A cap above the upstream connection pool can never shed — past the pool
        size requests queue for the 10s pool timeout, then get retried and failed
        over as if the PROVIDER had failed. Refuse the inversion at boot."""
        if not 1 <= v <= UPSTREAM_POOL_MAX_CONNECTIONS:
            raise ValueError(
                f"GATEWAY_MAX_CONCURRENT_STREAMS must be between 1 and "
                f"{UPSTREAM_POOL_MAX_CONNECTIONS} (the gateway's shared outbound "
                "connection-pool ceiling); a higher cap never sheds with 429 and "
                "degrades into retried pool timeouts instead"
            )
        return v

    @field_validator("compute_pod_name_prefix")
    @classmethod
    def _pod_prefix_is_a_real_prefix(cls, v: str) -> str:
        """The prefix IS the reconciler's blast radius: it decides which pods at
        the provider this deployment believes are its own and may terminate. An
        empty one makes every pod named ``-<12 hex>`` a candidate, and one
        carrying a hyphen, whitespace or upper case either collides with a
        sibling deployment's names or fails to match the pods we ourselves
        create. Refuse all of it at boot rather than discover it by reaping."""
        prefix = v.strip()
        if not prefix or prefix != v:
            raise ValueError(
                "COMPUTE_POD_NAME_PREFIX must be a non-empty name with no "
                "surrounding whitespace: it is the only thing separating this "
                "deployment's machines from another's on a shared provider account"
            )
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", prefix):
            raise ValueError(
                "COMPUTE_POD_NAME_PREFIX must be lowercase alphanumeric segments "
                f"joined by single hyphens (e.g. 'alkera', 'alkera-staging'); got {v!r}"
            )
        return prefix

    @field_validator("compute_reconcile_period_seconds")
    @classmethod
    def _reconcile_period_sane(cls, v: int) -> int:
        """Zero (or negative) is a Temporal schedule interval of zero, which is
        not "never" but "as fast as the worker can take it" — one provider
        listing per pass, against an account-wide API. A day means a leaked pod
        bills for a day. Refuse both ends."""
        if not 30 <= v <= 86_400:
            raise ValueError(
                "COMPUTE_RECONCILE_PERIOD_SECONDS must be between 30 and 86400 "
                "(how often the pod reconciliation pass runs)"
            )
        return v

    @field_validator("compute_shutdown_notice_secret")
    @classmethod
    def _notice_secret_strong(cls, v: SecretStr | None) -> SecretStr | None:
        """An empty value is "off"; a short one is a secret a drain could be
        forged under, so it is refused rather than accepted."""
        if v is None or not v.get_secret_value():
            return None
        if len(v.get_secret_value()) < 32:
            raise ValueError("COMPUTE_SHUTDOWN_NOTICE_SECRET must be at least 32 characters")
        return v

    @field_validator("compute_reconcile_grace_seconds", "compute_unconfirmed_create_seconds")
    @classmethod
    def _reconcile_window_sane(cls, v: int) -> int:
        """Both are the margin between "we cannot see an owner for this" and
        "destroy it" / "close its row". Zero removes the margin entirely, so a
        create still in flight is reaped on the tick it happens to overlap."""
        if not 60 <= v <= 86_400:
            raise ValueError(
                "COMPUTE_RECONCILE_GRACE_SECONDS and "
                "COMPUTE_UNCONFIRMED_CREATE_SECONDS must be between 60 and 86400: "
                "they are the margin that keeps an in-flight create from being "
                "reaped as an orphan"
            )
        return v

    @field_validator("compute_reconcile_max_pods", "compute_reconcile_max_terminations")
    @classmethod
    def _reconcile_ceiling_sane(cls, v: int) -> int:
        """A ceiling of zero would make every listing read as truncated (so
        nothing is ever written off) and every termination read as over budget
        (so no orphan is ever reaped) — the pass would run for ever and do
        nothing, silently."""
        if v < 1:
            raise ValueError(
                "COMPUTE_RECONCILE_MAX_PODS and COMPUTE_RECONCILE_MAX_TERMINATIONS "
                "must be at least 1; a zero ceiling disables the pass without "
                "saying so"
            )
        return v

    @field_validator(
        "gateway_upstream_max_attempts",
        "gateway_retry_after_cap_seconds",
        "gateway_retry_backoff_base_seconds",
        "gateway_retry_backoff_max_seconds",
        "gateway_upstream_connect_timeout_seconds",
        "gateway_upstream_write_timeout_seconds",
        "gateway_upstream_pool_timeout_seconds",
        "gateway_max_request_body_bytes",
        "gateway_per_principal_stream_share",
        "gateway_shed_retry_after_seconds",
        "gateway_stream_settle_margin_seconds",
        "gateway_stream_expiry_close_seconds",
        "gateway_stream_expiry_retry_seconds",
        "gateway_upstream_error_excerpt_chars",
    )
    @classmethod
    def _positive_gateway_bound(cls, v: float, info: ValidationInfo) -> float:
        """Each of these is a count, a size or a duration that only means something
        above zero. Zero or negative would not widen the bound, it would break the
        thing it sizes — no attempt ever made, a body limit nothing can satisfy, a
        backoff that hammers the provider, a shed that tells the caller to come
        straight back — so it is refused at boot rather than discovered on the
        first request."""
        if v <= 0:
            # A field validator always knows its own field; the fallback only
            # keeps the message well-formed if pydantic ever stops supplying it.
            name = (info.field_name or "gateway bound").upper()
            raise ValueError(f"{name} must be greater than zero (got {v})")
        return v

    @field_validator("gateway_retry_backoff_jitter_seconds")
    @classmethod
    def _jitter_is_not_negative(cls, v: float) -> float:
        """Zero is a legitimate setting — it makes the curve deterministic. A
        negative spread is not: it draws waits BELOW the computed backoff, so the
        retry that was meant to be spread out lands sooner than the un-jittered
        one, and `asyncio.sleep` of a negative returns immediately rather than
        raising, so nothing downstream would report it."""
        if v < 0:
            raise ValueError(
                f"GATEWAY_RETRY_BACKOFF_JITTER_SECONDS must not be negative (got {v}); "
                "use 0 for a deterministic curve"
            )
        return v

    @field_validator("gateway_max_idempotency_key_length")
    @classmethod
    def _idempotency_key_fits_the_column(cls, v: int) -> int:
        """The key is stored as the ProxyRequest's request_id, a VARCHAR(255). A
        ceiling above the column accepts a key the database then rejects mid-
        admission, which surfaces as a 500 on every retry of that request."""
        if not 1 <= v <= _REQUEST_ID_COLUMN_LENGTH:
            raise ValueError(
                f"GATEWAY_MAX_IDEMPOTENCY_KEY_LENGTH must be between 1 and "
                f"{_REQUEST_ID_COLUMN_LENGTH} (the width of the request-id column "
                "the key is persisted in)"
            )
        return v

    @field_validator("request_scan_max_body_bytes")
    @classmethod
    def _scan_ceiling_covers_real_bodies_without_holding_too_much(cls, v: int) -> int:
        """Below 64 KiB the scan stops covering the bodies it exists for — an
        ordinary chat message with its attachment metadata is already tens of
        kilobytes, so a smaller ceiling silently turns the boundary off for the
        routes that persist the most text. Above 32 MiB one unauthenticated
        request can make every process hold that much before any credential has
        been checked."""
        if not 65536 <= v <= 33554432:
            raise ValueError(
                "REQUEST_SCAN_MAX_BODY_BYTES must be between 65536 (below it the "
                "scan no longer covers ordinary request bodies) and 33554432 (above "
                "it one unauthenticated request buffers 32 MiB before any check)"
            )
        return v

    @model_validator(mode="after")
    def _retry_backoff_curve_is_ordered(self) -> Settings:
        """A ceiling below the first wait would make the curve shrink with each
        attempt, which reads as "retry harder the longer the provider is down"."""
        if self.gateway_retry_backoff_max_seconds < self.gateway_retry_backoff_base_seconds:
            raise ValueError(
                "GATEWAY_RETRY_BACKOFF_MAX_SECONDS must be at or above "
                "GATEWAY_RETRY_BACKOFF_BASE_SECONDS, or every retry waits the ceiling"
            )
        return self

    @model_validator(mode="after")
    def _settle_margin_within_the_stream_cap(self) -> Settings:
        """The stream is cut this far before the sweeper reclaims its hold. A margin
        at or above the cap leaves no stream at all — the pipeline's one-second
        floor would be the entire budget for every step."""
        if self.gateway_stream_settle_margin_seconds >= self.gateway_max_stream_seconds:
            raise ValueError(
                "GATEWAY_STREAM_SETTLE_MARGIN_SECONDS must be below "
                f"GATEWAY_MAX_STREAM_SECONDS ({self.gateway_max_stream_seconds}s), or a "
                "step is cut before it starts"
            )
        return self

    @model_validator(mode="after")
    def _expiry_retry_fits_inside_its_budget(self) -> Settings:
        """The watchdog waits a retry gap between close attempts and stops at the
        close budget. A gap at or above the budget spends the whole budget on the
        first wait, so the budget governs nothing and a wedged relay is given one
        retry whatever it is set to."""
        if self.gateway_stream_expiry_retry_seconds >= self.gateway_stream_expiry_close_seconds:
            raise ValueError(
                "GATEWAY_STREAM_EXPIRY_RETRY_SECONDS must be below "
                f"GATEWAY_STREAM_EXPIRY_CLOSE_SECONDS "
                f"({self.gateway_stream_expiry_close_seconds}s), or the close budget "
                "buys no retries"
            )
        return self

    @field_validator("alkera_temporal_task_queues")
    @classmethod
    def _known_task_queues(cls, v: str) -> str:
        """A typo'd queue name would boot a worker that polls nothing and serves no
        job forever, so an unknown or empty list is refused at boot."""
        names = [q.strip() for q in v.split(",") if q.strip()]
        if not names:
            raise ValueError(
                "ALKERA_TEMPORAL_TASK_QUEUES must name at least one queue "
                f"(valid: {', '.join(TASK_QUEUE_NAMES)})"
            )
        unknown = sorted(set(names) - set(TASK_QUEUE_NAMES))
        if unknown:
            raise ValueError(
                f"Unknown task queue(s) in ALKERA_TEMPORAL_TASK_QUEUES: {', '.join(unknown)}. "
                f"Valid: {', '.join(TASK_QUEUE_NAMES)}"
            )
        return v

    @field_validator("temporal_tls", mode="before")
    @classmethod
    def _blank_tls_means_auto(cls, v: object) -> object:
        """A seeded `.env` / task definition spells an unused knob as `TEMPORAL_TLS=`;
        that is "decide from the key", not a boolean parse error at boot."""
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("files_gc_enabled", mode="before")
    @classmethod
    def _blank_gc_flag_means_unset(cls, v: object) -> object:
        """`FILES_GC_ENABLED=` is "decide from APP_ENV", not a boolean parse
        error that stops every service from booting."""
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("alkera_worker_health_port")
    @classmethod
    def _health_port_sane(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError("ALKERA_WORKER_HEALTH_PORT must be a TCP port between 1 and 65535")
        return v

    @field_validator(
        "chat_answer_max_choices",
        "migration_lock_timeout_ms",
        "migration_runner_wait_seconds",
        "migration_batch_rows",
        "chat_refusal_reason_max_chars",
        "event_outbox_payload_max_bytes",
    )
    @classmethod
    def _chat_bound_positive(cls, v: int, info: ValidationInfo) -> int:
        # Zero and negative are not "no cap" on any of these: a zero page size
        # moves nothing for ever and a zero payload cap refuses every event.
        # Unbounded, where it is allowed at all, is spelled as an unset optional
        # instead.
        if v < 1:
            raise ValueError(f"{(info.field_name or '').upper()} must be at least 1")
        return v

    @field_validator("chat_message_max_attachments", "chat_max_attachments", mode="before")
    @classmethod
    def _attachment_ceiling_sane(cls, v: object, info: ValidationInfo) -> object:
        # Unset means "no ceiling of ours" — the schema's published one for a
        # message, none at all for a chat's own links; a number tightens. Blank
        # reads as unset rather than as a parse error, so a deployment can clear
        # a cap by emptying the variable. Zero would make every attachment
        # unusable while still accepting the link that created it.
        if isinstance(v, str) and not v.strip():
            return None
        if isinstance(v, str):
            v = int(v)
        if isinstance(v, int) and v < 1:
            raise ValueError(
                f"{(info.field_name or '').upper()} must be at least 1, or unset for no cap"
            )
        return v

    @field_validator("chat_model_catalog_timeout_seconds")
    @classmethod
    def _catalog_timeout_sane(cls, v: float) -> float:
        # A non-positive wait fails chat creation before the gateway is dialled.
        if v <= 0:
            raise ValueError(
                "CHAT_MODEL_CATALOG_TIMEOUT_SECONDS must be greater than 0 "
                "(how long chat creation waits on the gateway's model catalog)"
            )
        return v

    @field_validator("alkera_worker_max_concurrent_activities")
    @classmethod
    def _activity_concurrency_sane(cls, v: int) -> int:
        if not 1 <= v <= 100:
            raise ValueError(
                "ALKERA_WORKER_MAX_CONCURRENT_ACTIVITIES must be between 1 and 100 "
                "(activities in flight per served queue; size it against the DB pool)"
            )
        return v

    @field_validator("alkera_worker_connect_timeout_seconds")
    @classmethod
    def _connect_timeout_sane(cls, v: int) -> int:
        if not 1 <= v <= 3600:
            raise ValueError(
                "ALKERA_WORKER_CONNECT_TIMEOUT_SECONDS must be between 1 and 3600 "
                "(how long boot waits for the Temporal namespace before exiting)"
            )
        return v

    @property
    def temporal_tls_enabled(self) -> bool:
        """TLS to the Temporal frontend: the explicit setting when given, else on
        exactly when an API key is configured (a bearer key must never go in clear)."""
        if self.temporal_tls is not None:
            return self.temporal_tls
        return bool(self.temporal_api_key)

    @property
    def temporal_task_queue_list(self) -> list[str]:
        """The configured queues, trimmed and de-duplicated, order preserved."""
        seen: list[str] = []
        for name in (q.strip() for q in self.alkera_temporal_task_queues.split(",")):
            if name and name not in seen:
                seen.append(name)
        return seen

    @property
    def org_usage_metrics_set(self) -> frozenset[OrgUsageMetric]:
        keys = {k.strip() for k in self.org_usage_metrics.split(",") if k.strip()}
        if "all" in keys:
            return frozenset(OrgUsageMetric)
        return frozenset(OrgUsageMetric(k) for k in keys)

    @property
    def is_local(self) -> bool:
        return self.app_env == "local"

    @property
    def row_security_canary_enabled(self) -> bool:
        if self.row_security_canary is not None:
            return self.row_security_canary
        return self.app_env in ("staging", "production")

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_staging(self) -> bool:
        return self.app_env == "staging"

    @property
    def compute_provider_key_required(self) -> bool:
        """Whether a worker serving the compute meter must hold a provider key.

        The explicit setting wins; unset means "required off the laptop"."""
        if self.compute_require_provider_key is not None:
            return self.compute_require_provider_key
        return not self.is_local

    @property
    def app_env_is_explicit(self) -> bool:
        """Whether APP_ENV was given (environment, env file or argument) rather
        than defaulted. Only an explicit ``local`` unlocks the published dev
        secrets below."""
        return "app_env" in self.model_fields_set

    def _local_fallback(self, name: str, published: str) -> str:
        """The published dev value for ``name``, only when APP_ENV=local was set
        on purpose. A deployment that left APP_ENV unset would otherwise sign
        with a value anyone can read in the open repository."""
        if not self.app_env_is_explicit:
            raise MissingServerSecretError(
                f"{name} is not set and APP_ENV is not set. Set APP_ENV=production with "
                f"{name} (and the other server secrets) for a deployment, or "
                "APP_ENV=local for development."
            )
        return published

    def require_server_secrets(self) -> None:
        """Refuse to serve with a published dev secret: the backend and the
        gateway call this when they build their app, so a process that would
        sign sessions with one never starts."""
        _ = self.effective_jwt_secret
        _ = self.effective_token_hash_pepper
        _ = self.effective_files_content_signing_key

    @property
    def effective_jwt_secret(self) -> str:
        """JWT secret to use at runtime. Falls back to a known dev value only
        when APP_ENV=local was set explicitly (also when env supplies an empty
        string). In any other environment the validator below rejects
        missing/empty values."""
        if self.auth_jwt_secret:
            return self.auth_jwt_secret
        return self._local_fallback("AUTH_JWT_SECRET", _LOCAL_DEV_JWT_FALLBACK)

    @property
    def effective_jwt_secret_previous_list(self) -> list[str]:
        """Retired JWT secrets kept only to VERIFY already-issued tokens during a
        graceful rotation. Encode never uses these."""
        return [s.strip() for s in self.auth_jwt_secret_previous.split(",") if s.strip()]

    @property
    def secret_box_previous_keys_list(self) -> list[str]:
        """Retired secret-box keys kept only for DECRYPTION during a key rotation."""
        return [k.strip() for k in self.secret_box_keys_previous.split(",") if k.strip()]

    @property
    def effective_token_hash_pepper(self) -> str:
        """HMAC pepper for hashing single-use lookup tokens at rest. Falls back
        to a known dev value in local mode (also when env supplies an empty
        string). In any other environment `_validate_auth` rejects missing/empty
        values."""
        if self.token_hash_pepper:
            return self.token_hash_pepper
        return self._local_fallback("TOKEN_HASH_PEPPER", _LOCAL_DEV_TOKEN_HASH_PEPPER)

    @property
    def token_hash_pepper_previous_list(self) -> list[str]:
        """Retired peppers kept only for LOOKUP during a graceful pepper rotation."""
        return [p.strip() for p in self.token_hash_pepper_previous.split(",") if p.strip()]

    @property
    def google_oauth_configured(self) -> bool:
        return bool(self.oauth_google_client_id and self.oauth_google_client_secret)

    @property
    def github_oauth_configured(self) -> bool:
        return bool(self.oauth_github_client_id and self.oauth_github_client_secret)

    @property
    def gateway_proxy_mode(self) -> bool:
        """Whether the self-hosted gateway forwards to Alkera's hosted gateway
        (vs calling providers directly)."""
        return self.gateway_upstream.strip().lower() == "proxy"

    @property
    def gateway_proxy_configured(self) -> bool:
        """Proxy mode, an upstream URL and a token to authenticate with — all
        needed for the upstream forward to work."""
        return (
            self.gateway_proxy_mode
            and bool(self.alkera_proxy_url)
            and bool(self.alkera_proxy_token)
        )

    @property
    def stripe_configured(self) -> bool:
        """Paid flows can run end-to-end only when we can both CALL Stripe (secret
        key) AND VERIFY its webhooks (so a charge actually provisions credits).
        Gates the paid Checkout/Portal routes + the paid plan buttons; when False
        the product is Free-only."""
        return bool(self.stripe_secret_key and self.stripe_webhook_secret)

    @property
    def entitlements_minting_configured(self) -> bool:
        """Whether this deployment can MINT entitlement tokens (SaaS backend with
        the signing seed injected). Customer installs never have it."""
        return bool(self.alkera_entitlements_signing_key)

    @property
    def is_self_hosted(self) -> bool:
        """THE canonical signal for a customer self-hosted / on-prem deployment (vs
        the hosted SaaS). Use THIS (not a raw ``not stripe_configured``)
        wherever self-hosted-only behaviour branches — the ENTERPRISE plan, the hidden
        proxy-tokens page, the synced billing, the telemetry kill-switch, etc.

        An explicit ``SELF_HOSTED`` wins; otherwise it's inferred from Stripe (Alkera's
        SaaS always has keys, a self-host never does). The inference fails CLOSED — a
        component with no Stripe keys is treated as self-hosted, so we never phone
        telemetry home by accident. A SaaS component that legitimately lacks Stripe keys
        (e.g. the gateway) opts back in with ``SELF_HOSTED=false``."""
        if self.self_hosted is not None:
            return self.self_hosted
        return not self.stripe_configured

    @property
    def connections_reach_local_network_on(self) -> bool:
        """Whether a box's data connections reach as a self-hosted install's do
        (``alkera_core.connectors.reach.reaches_local_network``)."""
        if self.connections_reach_local_network is not None:
            return self.connections_reach_local_network
        return self.self_hosted is True

    @property
    def public_signup_open(self) -> bool:
        """Whether a person with no invitation may sign up and found an org."""
        if self.signup_mode is not None:
            return self.signup_mode == "open"
        return self.self_hosted is not True

    @property
    def ssh_machines_on(self) -> bool:
        """Whether an org admin may attach a machine by its SSH details."""
        if self.ssh_machines_enabled is not None:
            return self.ssh_machines_enabled
        return self.is_self_hosted

    @property
    def ssh_machines_private_ok(self) -> bool:
        """Whether an attached machine may be on a loopback or private address."""
        if self.ssh_machines_allow_private_addresses is not None:
            return self.ssh_machines_allow_private_addresses
        return self.is_self_hosted

    @property
    def oauth_redirect_base(self) -> str:
        """Base URL (no trailing slash) that providers redirect back to."""
        return self.api_public_base_url.rstrip("/")

    @property
    def turnstile_enabled(self) -> bool:
        """Whether to enforce the Turnstile captcha on the public auth endpoints.

        Off entirely when `turnstile_required` is False — the self-hosted / VPC /
        air-gapped opt-out: no key needed and NO calls to Cloudflare. Otherwise
        requires a secret key. In production the key is enough (the prod validator
        makes it mandatory unless opted out). In every NON-production env (local /
        staging) the captcha stays OFF even with a key configured — so a stray
        `.env.local` test key can't make every local login fail — UNLESS
        TURNSTILE_DEV_ENABLED is set to exercise the full challenge flow on purpose."""
        if not self.turnstile_required:
            return False
        if not self.turnstile_secret_key:
            return False
        return self.is_production or self.turnstile_dev_enabled

    @model_validator(mode="before")
    @classmethod
    def _fill_generated_secrets(cls, data: Any) -> Any:
        """Fill the server secrets left unset from ``GENERATED_SECRETS_DIR``,
        generating each one the first time. Runs before every check, so the
        production refusals judge the generated values like any other."""
        if not isinstance(data, dict) or not data.get("generated_secrets_dir"):
            return data
        directory = Path(str(data["generated_secrets_dir"]))
        filled = dict(data)
        for name in _GENERATED_SECRETS:
            if not _set(filled.get(name)):
                filled[name] = generated_secrets.load_or_create(directory, name.upper())
        # The secrets-at-rest key is otherwise derived from AUTH_JWT_SECRET, so
        # one appearing under an install that already stored credentials would
        # strand them. It is generated only beside a JWT secret this directory
        # holds (a fresh install), or read back once it was.
        if not _set(filled.get("secret_box_key")):
            if generated_secrets.read(directory, "AUTH_JWT_SECRET") == filled["auth_jwt_secret"]:
                filled["secret_box_key"] = generated_secrets.load_or_create(
                    directory, "SECRET_BOX_KEY", generated_secrets.fernet_key
                )
        return filled

    @model_validator(mode="after")
    def _validate_auth(self) -> Self:
        if not self.is_local and not self.auth_jwt_secret:
            raise ValueError(
                "AUTH_JWT_SECRET must be set (non-empty) when APP_ENV is not 'local'. "
                "Generate with: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
            )
        if not self.is_local and not self.token_hash_pepper:
            raise ValueError(
                "TOKEN_HASH_PEPPER must be set (non-empty) when APP_ENV is not 'local'. "
                "Generate with: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
            )
        # A content URL is a bearer capability over user bytes: its HMAC is the
        # only thing standing between a forged URL and another tenant's file. An
        # unset key falls back to a constant published in this repository
        # (`effective_files_content_signing_key`), so it is refused outside local
        # exactly like the JWT secret above — and, being an HMAC key rather than
        # a password, it also has to be wide enough to be worth attacking.
        if self.files_enabled and not self.is_local:
            files_key = (
                self.files_content_signing_key.get_secret_value()
                if self.files_content_signing_key is not None
                else ""
            )
            if not files_key.strip():
                raise ValueError(
                    "FILES_CONTENT_SIGNING_KEY must be set (non-empty) when FILES_ENABLED=true "
                    "and APP_ENV is not 'local': without it every content URL is signed with a "
                    "dev key published in the Alkera repository, so anyone could mint a URL for "
                    "any tenant's bytes. "
                    "Generate with: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
                )
            if len(files_key.encode()) < 32:
                raise ValueError(
                    "FILES_CONTENT_SIGNING_KEY must be at least 32 bytes "
                    f"(got {len(files_key.encode())}); it is the HMAC key over every content URL. "
                    "Generate with: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
                )
        return self

    @model_validator(mode="after")
    def _validate_tokenizer_warm(self) -> Self:
        """The startup tokenizer load is bounded in every environment. At zero or
        below the process would never wait and would always serve on the ratio;
        unbounded, a blackholed fetch would hold the boot open for as long as the
        network takes to give up, which is the failure the warm-up exists to
        prevent."""
        if not 0 < self.gateway_tokenizer_warm_timeout_seconds <= 120:
            raise ValueError(
                "GATEWAY_TOKENIZER_WARM_TIMEOUT_SECONDS must be greater than 0 and at "
                f"most 120 (got {self.gateway_tokenizer_warm_timeout_seconds!r}); it bounds "
                "how long a boot waits for the token encodings before serving without them"
            )
        return self

    @model_validator(mode="after")
    def _validate_device_poll_budget(self) -> Self:
        """The server tells a device-grant client how often to poll, so a poll
        throttle below that cadence refuses the clients that obey it. Checked in
        every environment: the failure is a login that dies mid-approval."""
        issued = math.ceil(60 / max(1, self.auth_device_poll_interval_seconds))
        for name in ("rate_limit_device_poll_per_minute", "rate_limit_device_poll_ip_per_minute"):
            if getattr(self, name) < issued:
                raise ValueError(
                    f"{name.upper()} must be at least {issued}: a client polling every "
                    f"{self.auth_device_poll_interval_seconds}s "
                    "(AUTH_DEVICE_POLL_INTERVAL_SECONDS) makes that many requests a minute"
                )
        return self

    @model_validator(mode="after")
    def _validate_realtime(self) -> Self:
        """The realtime knobs constrain each other (a keepalive slower than the
        stream deadline never fires; a presence TTL inside two keepalives makes a
        healthy peer flicker; a NOTIFY payload above 8000 bytes is refused by
        Postgres). Checked in every environment — a misconfigured local dev
        server would silently misbehave the same way."""
        errors = _validate_realtime_settings(self)
        if errors:
            joined = "\n  - ".join(errors)
            raise ValueError(
                f"Refusing to start with inconsistent realtime settings:\n  - {joined}"
            )
        return self

    @model_validator(mode="after")
    def _validate_oauth_mock(self) -> Self:
        """The mock login provider is a credential-less auth bypass — it must
        only ever exist in `local`. Refuse to boot `staging` OR `production`
        with it on (the production validator alone would miss staging)."""
        if self.oauth_mock_enabled and not self.is_local:
            raise ValueError(
                "OAUTH_MOCK_ENABLED must be 'false' outside local dev "
                f"(APP_ENV={self.app_env!r}); the mock provider bypasses real authentication"
            )
        return self

    @property
    def files_gc_active(self) -> bool:
        """Whether `files.gc` may collect: Files is served AND the pass is on."""
        if not self.files_enabled:
            return False
        if self.files_gc_enabled is None:
            return self.is_local
        return self.files_gc_enabled

    @model_validator(mode="after")
    def _validate_files_gc_max_orphan_fraction(self) -> Self:
        """The breaker's threshold is a fraction."""
        if not 0.0 <= self.files_gc_max_orphan_fraction <= 1.0:
            raise ValueError(
                "FILES_GC_MAX_ORPHAN_FRACTION must be between 0 and 1 (got "
                f"{self.files_gc_max_orphan_fraction})"
            )
        return self

    @model_validator(mode="after")
    def _validate_files_janitor_org_budget(self) -> Self:
        """A janitor pass must cover at least one org.

        A budget of zero would make every pass return the cursor it was handed
        and sweep nothing forever, which no test of a single pass would notice.
        """
        if self.files_janitor_org_budget < 1:
            raise ValueError(
                "FILES_JANITOR_ORG_BUDGET must be >= 1 (got "
                f"{self.files_janitor_org_budget}); a pass that sweeps no org "
                "never advances its cursor"
            )
        return self

    @model_validator(mode="after")
    def _validate_files_queued_recovery(self) -> Self:
        """The recovery of abandoned operations must take rows, and give up.

        A budget of zero is a net that catches nothing, silently: every tick
        would find no rows and report a clean pass, and a stranded bulk trash
        would look exactly as it did before the net existed. An allowance of
        zero is the opposite failure — every abandoned row failed on its first
        look, before a runner was ever given a chance.
        """
        if self.files_queued_recovery_budget < 1:
            raise ValueError(
                "FILES_QUEUED_RECOVERY_BUDGET must be >= 1 (got "
                f"{self.files_queued_recovery_budget}); a tick that takes no row "
                "recovers nothing and reports a clean pass"
            )
        if self.files_queued_recovery_attempts < 1:
            raise ValueError(
                "FILES_QUEUED_RECOVERY_ATTEMPTS must be >= 1 (got "
                f"{self.files_queued_recovery_attempts}); an operation would be "
                "failed before any runner was offered it"
            )
        return self

    @model_validator(mode="after")
    def _validate_files_tunable_bounds(self) -> Self:
        """Every tunable Files bound must still be a bound.

        Zero or a negative value would not widen the figure, it would invert
        it: a grant minted already expired, a page that may serve no request at
        all. The refusal names the env var so the operator who typed it reads
        back the name they set.
        """
        positive: list[tuple[str, int]] = [
            ("FILES_CONTENT_URL_TTL_SECONDS", self.files_content_url_ttl_seconds),
            ("FILES_PAGE_GRANT_TTL_SECONDS", self.files_page_grant_ttl_seconds),
            ("FILES_ARCHIVE_URL_TTL_SECONDS", self.files_archive_url_ttl_seconds),
            ("FILES_PAGE_GRANT_MAX_REQUESTS", self.files_page_grant_max_requests),
            ("FILES_ARCHIVE_PAGE_NODES", self.files_archive_page_nodes),
            ("FILES_ARCHIVE_MAX_RECORDED_SKIPS", self.files_archive_max_recorded_skips),
            ("FILES_DOWNLOAD_INLINE_MAX_NODES", self.files_download_inline_max_nodes),
        ]
        for name, value in positive:
            if value < 1:
                raise ValueError(
                    f"{name} must be >= 1 (got {value}); a bound of zero or "
                    "less is not a wider bound, it is one nothing can satisfy"
                )
        return self

    @model_validator(mode="after")
    def _validate_files_upload_ceiling(self) -> Self:
        """The published file ceiling must be the one a caller actually meets.

        An upload is cut into `files_part_max_bytes` parts and refused above
        `files_max_upload_parts` of them, so a deployment whose parts cannot
        carry `files_max_file_bytes` publishes a ceiling it will never honour:
        the caller reads the file limit, sends a file under it, and is refused
        for the part count instead — a number no answer led them to. The two
        ceilings are set here together, so the contradiction is refused here.
        """
        reachable = self.files_part_max_bytes * self.files_max_upload_parts
        if reachable < self.files_max_file_bytes:
            raise ValueError(
                f"FILES_PART_MAX_BYTES ({self.files_part_max_bytes}) x "
                f"FILES_MAX_UPLOAD_PARTS ({self.files_max_upload_parts}) is "
                f"{reachable} bytes, under FILES_MAX_FILE_BYTES "
                f"({self.files_max_file_bytes}); raise the part size so the "
                "published file ceiling is the one an upload reaches first"
            )
        return self

    @model_validator(mode="after")
    def _validate_files_store_driver(self) -> Self:
        """A configured provider must spell a driver the store table admits.

        The column's CHECK is enforced by Postgres on the first store row a
        deployment writes — mid-request, on the first drive of the first org —
        so a provider that has no row in `FILES_STORE_DRIVERS` is refused here
        instead of turning that request into a 500.
        """
        if self.files_store_provider not in FILES_STORE_DRIVERS:
            raise ValueError(
                f"FILES_STORE_PROVIDER={self.files_store_provider!r} maps to no "
                "file_stores.driver; add it to FILES_STORE_DRIVERS (and to the "
                "model's STORE_DRIVERS catalogue if the driver itself is new)"
            )
        return self

    @model_validator(mode="after")
    def _validate_staging(self) -> Self:
        """Lighter guardrails for staging / per-PR preview environments.

        Staging is internet-facing (we front it with Okta / Cloudflare Access),
        so session cookies must be Secure and the frontend origin must be real
        (it ends up in invitation / password-reset email links). But a per-PR
        preview runs the full local-style compose stack, so Mailpit and the
        in-box Postgres ARE allowed here — unlike production. Runs after
        `_validate_oauth_mock`, so the mock-auth bypass error still fires first.
        """
        if not self.is_staging:
            return self

        errors: list[str] = []
        if not self.auth_cookie_secure:
            errors.append("AUTH_COOKIE_SECURE must be 'true' in staging (HTTPS-only cookies)")
        if self.api_cors_origins == "http://localhost:5173":
            errors.append("API_CORS_ORIGINS must list the staging frontend origin(s)")
        if self.frontend_base_url == "http://localhost:5173":
            errors.append(
                "FRONTEND_BASE_URL must be the staging frontend URL (used in email links)"
            )
        # Staging is where billing is rehearsed, against Stripe's TEST mode: a
        # live key here would charge real cards from a rehearsal, so it is
        # refused the way production refuses a test key. The webhook secret is
        # required for the same reason it is in production — an unverifiable
        # event must never provision credits — and a test-mode endpoint has
        # its own secret, never production's.
        if self.stripe_secret_key:
            if not self.stripe_secret_key.startswith(("sk_test_", "rk_test_")):
                errors.append(
                    "STRIPE_SECRET_KEY must be a test-mode key (sk_test_… / rk_test_…) in "
                    "staging, never a live key"
                )
            if not self.stripe_webhook_secret:
                errors.append(
                    "STRIPE_WEBHOOK_SECRET must be set when STRIPE_SECRET_KEY is (the "
                    "staging endpoint's own test-mode signing secret)"
                )
        if self.stripe_publishable_key and not self.stripe_publishable_key.startswith("pk_test_"):
            errors.append("STRIPE_PUBLISHABLE_KEY must be a test-mode key (pk_test_…) in staging")
        if errors:
            joined = "\n  - ".join(errors)
            raise ValueError(
                f"Refusing to start in APP_ENV=staging with unconfigured settings:\n  - {joined}"
            )
        return self

    @model_validator(mode="after")
    def _validate_production(self) -> Self:
        """Refuse to start a production app with insecure defaults.

        Every check here fires only when `APP_ENV=production`. Local keeps its
        leniency; staging has its own lighter validator (`_validate_staging`) so
        previews can run the local-style stack. Each error names the env var the
        operator must set.
        """
        if not self.is_production:
            return self

        errors: list[str] = []
        if not self.realtime_crdt_unsaved_sweep_enabled:
            errors.append(
                "REALTIME_CRDT_UNSAVED_SWEEP_ENABLED must not be false (it is what writes back the "
                "edits of a process that stopped between an edit and its write back)"
            )
        if self.database_idle_in_transaction_timeout_ms == 0:
            errors.append(
                "DATABASE_IDLE_IN_TRANSACTION_TIMEOUT_MS must not be 0 (a request that dies inside "
                "a transaction would hold its row locks for as long as its connection lives)"
            )
        if not self.auth_cookie_secure:
            errors.append("AUTH_COOKIE_SECURE must be 'true' (session cookies must be HTTPS-only)")
        # The access token is stateless between revocation checks, so its TTL is
        # the window a stolen cookie works on its own. The refresh cookie keeps the
        # user signed in; the access token has no reason to outlive an hour.
        if self.auth_token_ttl_seconds > self.auth_token_ttl_production_max_seconds:
            errors.append(
                "AUTH_TOKEN_TTL_SECONDS must be at most "
                f"{self.auth_token_ttl_production_max_seconds} (one hour) in production — "
                "the access token is short-lived; the refresh cookie keeps the browser "
                "signed in"
            )
        # The idle and absolute windows are the standard pair: activity moves the
        # idle one, nothing moves the absolute one. An idle window at or past the
        # absolute lifetime is not a policy — it is the absolute lifetime with a
        # dead setting beside it, and it reads as an idle timeout that is silently
        # never reached.
        if self.auth_refresh_idle_seconds >= self.auth_refresh_absolute_seconds:
            errors.append(
                "AUTH_REFRESH_IDLE_SECONDS must be below AUTH_REFRESH_ABSOLUTE_SECONDS — "
                "an idle window the absolute lifetime ends first can never expire a session"
            )
        if (
            self.auth_idle_timeout_seconds > 0
            and self.auth_idle_timeout_seconds >= self.auth_refresh_absolute_seconds
        ):
            errors.append(
                "AUTH_IDLE_TIMEOUT_SECONDS must be below AUTH_REFRESH_ABSOLUTE_SECONDS — "
                "an idle window the absolute lifetime ends first can never expire a session"
            )
        # The absolute lifetime is the one bound no activity can move, so it is
        # what caps a compromised browser session. Thirty days is the ceiling a
        # sliding session is allowed to reach.
        if self.auth_refresh_reuse_grace_seconds > AUTH_REFRESH_REUSE_GRACE_MAX_SECONDS:
            errors.append(
                "AUTH_REFRESH_REUSE_GRACE_SECONDS must be at most "
                f"{AUTH_REFRESH_REUSE_GRACE_MAX_SECONDS} in production; inside the grace a "
                "copied refresh token is not detected"
            )
        if self.auth_refresh_absolute_seconds > AUTH_ABSOLUTE_LIFETIME_MAX_SECONDS:
            errors.append(
                "AUTH_REFRESH_ABSOLUTE_SECONDS must be at most "
                f"{AUTH_ABSOLUTE_LIFETIME_MAX_SECONDS} (thirty days) in production — "
                "it is the only bound activity cannot move"
            )
        # A chat idle window of minutes is a test setting: served to production
        # boxes it would put every reader's chat to sleep between two questions.
        if self.compute_chat_idle_minutes < CHAT_IDLE_PRODUCTION_FLOOR_MINUTES:
            errors.append(
                f"COMPUTE_CHAT_IDLE_MINUTES must be at least {CHAT_IDLE_PRODUCTION_FLOOR_MINUTES} "
                "in production (a shorter window is a test setting; it would sleep a reader's "
                "chat between two questions)"
            )
        # The per-route throttle is the burst half of the abuse control (the edge
        # WAF counts over five minutes and never sees a one-second burst), so a
        # production API with it off is an API with no burst control at all.
        if not self.rate_limit_enabled:
            errors.append(
                "RATE_LIMIT_ENABLED must be 'true' in production (the edge WAF bounds "
                "sustained volume only; the app-level classes are the burst control)"
            )
        # The `fast` profile hashes at argon2's floor (8 KiB, one pass, one lane),
        # which is no barrier at all to whoever ends up holding the password
        # column. It exists so test suites don't pay the real cost per fixture.
        if self.password_hash_profile != "production":  # noqa: S105
            errors.append(
                "PASSWORD_HASH_PROFILE must be 'production' (the 'fast' profile hashes at "
                "argon2's floor and exists only for test suites -- a stolen password "
                "column hashed with it is a cracked password column)"
            )
        if self.email_enabled and self.smtp_host in ("", "localhost"):
            errors.append(
                "SMTP_HOST must be a real relay (not blank or the local-dev default "
                "'localhost') — or set EMAIL_ENABLED=false to run with no email at all"
            )
        # Production relays (SES, Resend, …) all authenticate. Empty == unset (the
        # alkera/<env> secret seeds these as ""), so `not` catches both None and "".
        # SMTP auth is OPTIONAL in production: many enterprise / on-prem relays are
        # internal, IP-allowlisted smart hosts that accept mail with no credentials.
        # A half-set credential, though, is a misconfiguration — require both or
        # neither (a relay that DOES need auth, like SES/Resend, gets both).
        if bool(self.smtp_username) != bool(self.smtp_password):
            errors.append(
                "SMTP_USERNAME and SMTP_PASSWORD must be set together "
                "(or set neither, for an anonymous internal relay)"
            )
        if self.database_url.startswith("postgresql+asyncpg://alkera:alkera@localhost"):
            errors.append(
                "DATABASE_URL must point at the production Postgres (not the local dev default)"
            )
        if self.api_cors_origins == "http://localhost:5173":
            errors.append("API_CORS_ORIGINS must list the production frontend origin(s)")
        # The portal URL behind every emailed link, the device grant and the CSRF
        # trusted set; its own block so the secret checks stay separate.
        errors.extend(_frontend_base_url_errors(self.frontend_base_url))
        # The gate-ingest cap bounds what ONE unauthenticated request can make
        # FastAPI buffer on the WAF-exempt upload paths, so its VALUE is the
        # defense, bounded BOTH ways: below the 32 MiB artifact ceiling
        # (gate_service.MAX_ARTIFACT_BYTES) every valid upload 413s; above it + 2
        # MiB an inflated override multiplies pre-auth memory (0/negative 413s all).
        if not (33554432 <= self.gate_ingest_max_body_bytes <= 35651584):
            errors.append(
                "GATE_INGEST_MAX_BODY_BYTES must be between 33554432 (the 32 MiB gate "
                "artifact ceiling -- a smaller cap 413s every valid upload) and 35651584 "
                "(that ceiling plus 2 MiB of envelope headroom -- a larger cap multiplies "
                "the pre-auth memory one unauthenticated request can pin)"
            )
        # The resident budget decides how many upload parts a process admits at
        # once. Below one part's cost every part PUT is a 503 -- an upload
        # surface that refuses everything, not a limit.
        if self.files_upload_resident_budget_bytes < FILES_UPLOAD_PART_RESIDENT_BYTES:
            errors.append(
                "FILES_UPLOAD_RESIDENT_BUDGET_BYTES must be at least "
                f"{FILES_UPLOAD_PART_RESIDENT_BYTES} (the resident cost of one streaming "
                "part -- a smaller budget sheds every upload part with a 503)"
            )
        # The socket byte windows ARE the bound on how much JSON one connection
        # can make a worker parse, so their VALUES are the defense. At zero or
        # below every socket is closed on its first frame (a self-inflicted
        # outage), and a publisher held to a tighter budget than a person's
        # socket is the same outage for the box streaming a turn -- which is the
        # lane that legitimately sends the most.
        if self.realtime_ws_max_bytes_per_window <= 0:
            errors.append(
                "REALTIME_WS_MAX_BYTES_PER_WINDOW must be positive (at zero every socket "
                "is closed on its first frame); it bounds the JSON one connection can make "
                "a worker parse per window"
            )
        if self.realtime_ws_publisher_max_bytes_per_window < self.realtime_ws_max_bytes_per_window:
            errors.append(
                "REALTIME_WS_PUBLISHER_MAX_BYTES_PER_WINDOW must be at least "
                "REALTIME_WS_MAX_BYTES_PER_WINDOW — a workspace machine publishing a chat "
                "streams far more than a person's socket, and a tighter budget closes it "
                "mid-turn"
            )
        if self.realtime_ws_publisher_max_frames_per_window <= 0:
            errors.append(
                "REALTIME_WS_PUBLISHER_MAX_FRAMES_PER_WINDOW must be positive (at zero "
                "every workspace machine is closed on its first frame, losing the chat it "
                "was publishing)"
            )
        # The scrape credential is a bearer token on an internet-reachable host, so
        # it needs real entropy (same floors as the other used-as-secret values).
        # Leaving it unset is a valid choice — /metrics then answers 404 outside
        # local (see alkera_core.observability.asgi), it just isn't scrapable.
        # Empty == unset, like the other secrets above: the alkera/<env> secret
        # seeds every key as "", and the runtime guard reads it the same way, so a
        # truthiness test here (not `is not None`) is what keeps the boot check and
        # the request check agreeing. A blank-but-non-empty value is still a
        # misconfiguration and still fails.
        if self.metrics_auth_token and (
            len(self.metrics_auth_token) < 16 or len(set(self.metrics_auth_token)) < 5
        ):
            errors.append(
                "METRICS_AUTH_TOKEN must be a high-entropy secret: at least 16 characters "
                "and at least 5 distinct characters (it is the only thing standing between "
                "the internet and /metrics) — or unset it to serve no metrics at all"
            )
        # Bot protection on the public auth endpoints is mandatory in prod. Empty
        # == unset (the alkera/<env> secret seeds it as ""), so `not` catches both
        # None and "". The public site key is a frontend build var, checked in the
        # SPA build, not here.
        if self.turnstile_required and not self.turnstile_secret_key:
            errors.append(
                "TURNSTILE_SECRET_KEY must be set in production "
                "(Cloudflare Turnstile guards login / signup / password-reset) — "
                "or set TURNSTILE_REQUIRED=false to run without it (self-hosted / "
                "air-gapped installs with a trusted, internal signup surface)"
            )
        # OAUTH_MOCK_ENABLED is enforced for all non-local envs by
        # `_validate_oauth_mock` above (covers staging too).
        # Each OAuth provider needs BOTH halves of its credential or neither.
        if bool(self.oauth_google_client_id) != bool(self.oauth_google_client_secret):
            errors.append(
                "OAUTH_GOOGLE_CLIENT_ID and OAUTH_GOOGLE_CLIENT_SECRET must be set together"
            )
        if bool(self.oauth_github_client_id) != bool(self.oauth_github_client_secret):
            errors.append(
                "OAUTH_GITHUB_CLIENT_ID and OAUTH_GITHUB_CLIENT_SECRET must be set together"
            )
        # If any provider is wired up, the callback base must be a real URL.
        if (self.google_oauth_configured or self.github_oauth_configured) and (
            self.api_public_base_url == "http://localhost:8000"
        ):
            errors.append(
                "API_PUBLIC_BASE_URL must be the public backend origin when an OAuth "
                "provider is configured (not the local dev default)"
            )
        # Stripe is NOT required (a Free-only prod is valid), but a partial or
        # test-mode config in prod is a money bug — enforce coherence.
        if self.stripe_secret_key:
            if self.stripe_secret_key.startswith(("sk_test_", "rk_test_")):
                errors.append(
                    "STRIPE_SECRET_KEY must be a live key (sk_live_… / rk_live_…) in production, "
                    "not a test-mode key"
                )
            if not self.stripe_webhook_secret:
                errors.append(
                    "STRIPE_WEBHOOK_SECRET must be set when STRIPE_SECRET_KEY is "
                    "(an unverifiable charge would never provision credits)"
                )
        if self.stripe_publishable_key and self.stripe_publishable_key.startswith("pk_test_"):
            # The browser would open Checkout against the test account while the
            # server charged live: a paid plan nobody could buy, or the reverse.
            errors.append(
                "STRIPE_PUBLISHABLE_KEY must be a live key (pk_live_…) in production, "
                "not a test-mode key"
            )
        if (
            self.stripe_price_plus_monthly or self.stripe_price_pro_monthly
        ) and not self.stripe_secret_key:
            errors.append(
                "A STRIPE_PRICE_* is set but STRIPE_SECRET_KEY is not — paid tiers can't be sold"
            )

        errors.extend(_validate_egress_settings(self))
        # Gateway upstream mode: only the two understood values, and proxy mode
        # needs a token to authenticate to Alkera's hosted gateway with.
        if self.gateway_upstream.strip().lower() not in ("direct", "proxy"):
            errors.append("GATEWAY_UPSTREAM must be 'direct' or 'proxy'")
        if self.gateway_proxy_mode and not self.alkera_proxy_token:
            errors.append(
                "ALKERA_PROXY_TOKEN must be set when GATEWAY_UPSTREAM=proxy "
                "(the org-scoped token the upstream deployment issued for this gateway)"
            )
        if (self.gateway_proxy_mode or self.alkera_proxy_token) and not self.alkera_proxy_url:
            errors.append(
                "ALKERA_PROXY_URL must be set when GATEWAY_UPSTREAM=proxy or "
                "ALKERA_PROXY_TOKEN is set (the upstream gateway's base URL)"
            )
        # Per-step bounds. A silence bound short enough to cut a provider that is
        # merely thinking turns a healthy step into a failed one, and the failure
        # looks like a provider outage — so a minute is the floor, generous
        # against a network hiccup and far under any real reasoning pause. (A
        # dead peer is caught by the transport's TCP keepalive probes, not by
        # this bound, so lowering it buys no crash detection.)
        for name, silence in (
            ("GATEWAY_UPSTREAM_READ_TIMEOUT_SECONDS", self.gateway_upstream_read_timeout_seconds),
            ("GATEWAY_BEDROCK_READ_TIMEOUT_SECONDS", self.gateway_bedrock_read_timeout_seconds),
        ):
            if silence < _MIN_UPSTREAM_SILENCE_SECONDS:
                errors.append(
                    f"{name} must be at least {_MIN_UPSTREAM_SILENCE_SECONDS:g}s "
                    "(a shorter bound cuts a provider that is only thinking)"
                )
        # A stream cap at or below the keepalive cadence means the stream is cut
        # before its first keepalive ever fires, so the mechanism that exists to
        # keep a quiet step alive across the edge never runs once.
        if self.gateway_max_stream_seconds <= self.gateway_client_keepalive_seconds:
            errors.append(
                "GATEWAY_MAX_STREAM_SECONDS must exceed GATEWAY_CLIENT_KEEPALIVE_SECONDS "
                f"({self.gateway_client_keepalive_seconds:g}s) — a stream cut before its "
                "first keepalive never gets one"
            )
        # /health/ready's own budget must fit inside the probe reading it. At or
        # above the load balancer's timeout the probe gives up first, so a slow
        # database reads as an unreachable host and every task drains at once —
        # the opposite of the fast 503 the endpoint exists to return. 30s is the
        # widest health-check timeout the load balancers this ships behind allow.
        if self.health_ready_timeout_seconds <= 0:
            errors.append(
                "HEALTH_READY_TIMEOUT_SECONDS must be greater than 0 (at or below zero "
                "/health/ready answers 503 before the database is asked anything)"
            )
        elif self.health_ready_timeout_seconds >= _MAX_HEALTH_CHECK_TIMEOUT_SECONDS:
            errors.append(
                "HEALTH_READY_TIMEOUT_SECONDS must be under "
                f"{_MAX_HEALTH_CHECK_TIMEOUT_SECONDS:g}s, the widest health-check timeout a "
                "load balancer in front of this service allows — at or above it the probe "
                "gives up first and a slow database drains every task instead of "
                "answering 503"
            )
        if self.health_ready_grace_seconds < 0:
            errors.append(
                "HEALTH_READY_GRACE_SECONDS must be 0 (the strict probe) or more — a negative "
                "window cannot be measured"
            )
        # The keepalive exists to stop the edge closing a silent stream, so a
        # cadence at or above the edge's own idle window never fires in time and
        # the step is torn by the load balancer instead of by the gateway.
        if self.gateway_client_keepalive_seconds <= 0:
            errors.append(
                "GATEWAY_CLIENT_KEEPALIVE_SECONDS must be greater than 0 in production "
                "(a silent step outlives any edge idle timeout without it)"
            )
        elif self.gateway_client_keepalive_seconds >= self.gateway_edge_idle_timeout_seconds:
            errors.append(
                "GATEWAY_CLIENT_KEEPALIVE_SECONDS must be under "
                "GATEWAY_EDGE_IDLE_TIMEOUT_SECONDS "
                f"({self.gateway_edge_idle_timeout_seconds:g}s) — the edge closes a "
                "connection it has seen no bytes on for that long"
            )
        elif (
            self.gateway_client_keepalive_seconds * GATEWAY_KEEPALIVES_PER_CLIENT_SILENCE
            > GATEWAY_CLIENT_SILENCE_SECONDS
        ):
            # The agent on a box reads a step that went this quiet as a dead
            # connection and ends it, so the keepalive must speak several times
            # inside that window or a healthy thinking step is cut.
            errors.append(
                "GATEWAY_CLIENT_KEEPALIVE_SECONDS must be at most "
                f"{GATEWAY_CLIENT_SILENCE_SECONDS / GATEWAY_KEEPALIVES_PER_CLIENT_SILENCE:g}s "
                f"(a client ends a step it hears nothing on for "
                f"{GATEWAY_CLIENT_SILENCE_SECONDS:g}s)"
            )
        # The postpaid guardrail must at least cover the slack a settle is already
        # allowed to take past its own hold; under that, a postpaid pool can be
        # left unable to admit the very next request it just overspent on.
        # (compared in USD — this module cannot import alkera_core.money,
        # which imports settings; 1e9 is the nano-USD scale the sibling name states)
        overdraft_usd = Decimal(self.billing_max_overdraft_nanos) / Decimal(1_000_000_000)
        if self.billing_postpaid_reserve_buffer_usd < overdraft_usd:
            errors.append(
                "BILLING_POSTPAID_RESERVE_BUFFER_USD must be at least "
                f"BILLING_MAX_OVERDRAFT_NANOS (${overdraft_usd:f})"
            )
        # A stale window no wider than two touches reclaims the hold of a stream
        # that is still running — its settle then no-ops and the usage is free.
        if self.billing_reservation_touch_seconds <= 0:
            errors.append("BILLING_RESERVATION_TOUCH_SECONDS must be greater than 0")
        elif self.billing_reservation_stale_seconds <= 2 * self.billing_reservation_touch_seconds:
            errors.append(
                "BILLING_RESERVATION_STALE_SECONDS must exceed twice "
                "BILLING_RESERVATION_TOUCH_SECONDS (a live stream must survive a missed touch)"
            )
        # A custom outbound CA must resolve at boot (fail fast, not on the first
        # OAuth/provider call). Accept inline PEM or a readable file path.
        if (
            self.outbound_ca_bundle
            and "BEGIN CERTIFICATE" not in self.outbound_ca_bundle
            and not Path(self.outbound_ca_bundle).is_file()
        ):
            errors.append("OUTBOUND_CA_BUNDLE must be inline PEM text or a readable PEM file path")
        errors.extend(_validate_temporal_settings(self))
        errors.extend(_validate_files_settings(self))
        # Certificate verification needs the server's CA to check against — and the
        # engine loads it at import time, so an unreadable path must fail at boot here
        # with a clear message rather than a cryptic import error later.
        if self.database_sslmode in ("verify-ca", "verify-full"):
            if not self.database_sslrootcert:
                errors.append(
                    "DATABASE_SSLROOTCERT must be set when "
                    f"DATABASE_SSLMODE={self.database_sslmode} "
                    "(the path to the Postgres server's CA certificate)"
                )
            elif not Path(self.database_sslrootcert).is_file():
                errors.append(
                    f"DATABASE_SSLROOTCERT={self.database_sslrootcert!r} is not a readable file"
                )
        errors.extend(_production_secret_errors(self))
        # A customer-managed secret-box key (+ any retired keys) must be valid Fernet
        # keys, or secrets-at-rest encryption fails on first use — validate at boot.
        from cryptography.fernet import Fernet

        secret_box_keys = [("SECRET_BOX_KEY", self.secret_box_key)] if self.secret_box_key else []
        secret_box_keys += [
            (f"SECRET_BOX_KEYS_PREVIOUS[{i}]", k)
            for i, k in enumerate(self.secret_box_previous_keys_list)
        ]
        for label, key in secret_box_keys:
            try:
                Fernet(key.encode("ascii"))
            except (ValueError, TypeError):
                errors.append(f"{label} must be a urlsafe-base64-encoded 32-byte Fernet key")

        # A misconfigured SaaS-side entitlement SIGNING seed fails loud at boot
        # (minting would 500 later otherwise). The customer-set token
        # (ALKERA_ENTITLEMENTS) is deliberately NOT validated anywhere — a bad
        # token must never take a customer install down.
        if self.alkera_entitlements_signing_key:
            import base64 as _b64
            import binascii as _binascii

            seed = self.alkera_entitlements_signing_key
            try:
                raw = _b64.urlsafe_b64decode(seed + "=" * (-len(seed) % 4))
            except (ValueError, _binascii.Error):
                raw = b""
            if len(raw) != 32:
                errors.append(
                    "ALKERA_ENTITLEMENTS_SIGNING_KEY must be a base64url-encoded "
                    "raw 32-byte Ed25519 seed (an Ed25519 private key)"
                )

        if errors:
            joined = "\n  - ".join(errors)
            raise ValueError(
                "Refusing to start in APP_ENV=production with insecure / unconfigured settings:\n"
                f"  - {joined}"
            )
        return self


def _validate_egress_settings(settings: Settings) -> list[str]:
    """Production refusal for an outbound path that cannot keep its promise.

    A fetch of a URL somebody else supplied is vetted AND pinned: the address the
    guard classified is the address dialed, so a record that answers public once
    and private the next time gains nothing. A forward proxy takes the connection
    away from this process, and with it the pin — the proxy does its own lookup.
    Today that downgrade is silent, which is the worst of the three options. An
    operator who needs the proxy says so with EGRESS_ALLOW_PROXY; one who does
    not gets told at boot rather than discovering it in an incident.
    """
    from alkera_core.egress import configured_proxy_env

    names = configured_proxy_env()
    if not names or settings.egress_allow_proxy:
        return []
    return [
        f"{', '.join(names)} routes outbound traffic through a forward proxy, which "
        "makes the connection itself — so a fetch of a URL a user or an org admin "
        "supplied is vetted but NOT pinned to the address that was vetted. Set "
        "EGRESS_ALLOW_PROXY=true to accept that, or unset the proxy variables"
    ]


def _validate_files_settings(settings: Settings) -> list[str]:
    """Production refusals for the Files object-storage layer.

    Only reachable when ``FILES_ENABLED=true``: a deployment that does not serve
    Files must not be blocked by a store it never configured. Each refusal is a
    combination that would either lose user bytes or put the session cookie on
    user-controlled content.
    """
    errors: list[str] = []
    # Checked before the `files_enabled` gate: this flag decides WHERE queued
    # work runs, and a SaaS replica that runs a 40,000-node copy inside the
    # request that asked for it holds a worker thread and a transaction for the
    # length of the copy. A single-process self-hosted install has no Temporal
    # worker to hand it to, so that shape is the one exemption.
    if settings.files_inline_operations and not settings.is_self_hosted:
        errors.append(
            "FILES_INLINE_OPERATIONS must be 'false' on SaaS: a queued operation "
            "(an upload promote, a copy, an oversized move) belongs to the Temporal "
            "worker, not to the request that queued it. Set SELF_HOSTED=true for a "
            "single-process install that has no worker"
        )
    if not settings.files_enabled:
        return errors

    content_host = _host_of(settings.files_content_base_url)
    if content_host is None:
        errors.append(
            "FILES_CONTENT_BASE_URL must be the separate origin user content is served "
            "from (e.g. https://c.alkerausercontent.com) — serving user bytes from the "
            "API or SPA hostname puts the session cookie on attacker-controlled content"
        )
    else:
        app_hosts = {
            "API_PUBLIC_BASE_URL": _host_of(settings.api_public_base_url),
            "FRONTEND_BASE_URL": _host_of(settings.frontend_base_url),
        }
        clashes = sorted(name for name, host in app_hosts.items() if host == content_host)
        if clashes:
            errors.append(
                f"FILES_CONTENT_BASE_URL host {content_host!r} is also "
                f"{' and '.join(clashes)} — cookies ignore ports and are scoped by "
                "hostname, so the content domain must be a different registrable host"
            )

    capabilities = settings.files_store_capabilities_hint
    if (
        settings.files_transfer_mode == "proxied"
        and settings.files_store_provider == "filesystem"
        and not settings.is_self_hosted
    ):
        errors.append(
            "FILES_TRANSFER_MODE=proxied with FILES_STORE_PROVIDER=filesystem is refused "
            "on SaaS: every replica would have to share one filesystem volume. Use an "
            "object store, or set SELF_HOSTED=true for a single-volume install"
        )
    if settings.files_transfer_mode == "direct" and "presigned_urls" not in capabilities:
        errors.append(
            f"FILES_TRANSFER_MODE=direct needs a store that can presign URLs; "
            f"FILES_STORE_PROVIDER={settings.files_store_provider} cannot — "
            "use FILES_TRANSFER_MODE=proxied"
        )
    if settings.files_store_provider == "aws" and not settings.files_vend_role_arn:
        errors.append(
            "FILES_VEND_ROLE_ARN must be set with FILES_STORE_PROVIDER=aws (the role "
            "session-tag-scoped tenant credentials are vended from; without it every "
            "tenant handle would carry the task role's full bucket access)"
        )
    if (
        settings.files_store_provider in ("s3_compatible", "aws")
        and not settings.files_store_bucket
    ):
        errors.append(
            f"FILES_STORE_BUCKET must be set with "
            f"FILES_STORE_PROVIDER={settings.files_store_provider}"
        )
    if bool(settings.files_store_access_key) != bool(
        settings.files_store_secret_key and settings.files_store_secret_key.get_secret_value()
    ):
        errors.append(
            "FILES_STORE_ACCESS_KEY and FILES_STORE_SECRET_KEY must be set together "
            "(or set neither, to use the instance/task role)"
        )
    # The local store credentials are generated into every worktree's
    # .env.workspace, so they are the pair a production boot is most likely to
    # inherit by accident — and they are committed, so anyone could then read and
    # write the bucket. Either half is enough to refuse.
    secret = settings.files_store_secret_key
    if settings.files_store_access_key == _LOCAL_DEV_FILES_STORE_ACCESS_KEY or (
        secret is not None and secret.get_secret_value() == _LOCAL_DEV_FILES_STORE_SECRET_KEY
    ):
        errors.append(
            "FILES_STORE_ACCESS_KEY / FILES_STORE_SECRET_KEY still hold the committed "
            "local-dev SeaweedFS credentials (deploy/docker/seaweedfs-s3.json, written "
            "into .env.workspace for local dev) — set this deployment's own keys, or "
            "unset both to use the instance/task role"
        )
    if settings.files_store_root is not None and _under_ephemeral_root(settings.files_store_root):
        errors.append(
            f"FILES_STORE_ROOT={str(settings.files_store_root)!r} is under an OS scratch "
            "directory — it is wiped on reboot, so committed user bytes would be lost"
        )
    return errors


#: Values published in this repository as local-dev fallbacks. A production
#: secret equal to any of them is known to everyone who can read the source, in
#: whichever slot it is pasted.
_PUBLISHED_DEV_SECRETS: Final = frozenset(
    {
        _LOCAL_DEV_JWT_FALLBACK,
        _LOCAL_DEV_TOKEN_HASH_PEPPER,
        _LOCAL_DEV_FILES_CONTENT_SIGNING_KEY,
        _LOCAL_DEV_FILES_STORE_SECRET_KEY,
    }
)

#: The floor on an operator-chosen server secret: 32 bytes, the width of the
#: HMAC-SHA256 key each of these is. A length floor, not an entropy heuristic --
#: a long passphrase from a password manager is a legitimate value.
_PRODUCTION_SECRET_MIN_BYTES: Final = 32

_HOW_TO_GENERATE: Final = (
    "Generate with: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
)


def _production_secret_errors(settings: Settings) -> list[str]:
    """Refuse a published dev value, or one under 32 bytes, in the server
    secrets the other checks only require to be non-empty.

    ``AUTH_JWT_SECRET`` signs every session (HS256, so a weak one is cracked
    offline from any user's own cookie) and, with no ``SECRET_BOX_KEY``, is
    also the root of the secrets-at-rest key. ``TOKEN_HASH_PEPPER`` keys the
    stored token digests. ``FILES_CONTENT_SIGNING_KEY`` signs every content URL
    (its width is checked in ``_validate_auth``; its published dev value is
    refused here). The ``*_PREVIOUS`` lists still verify, so an old value is
    as dangerous as the current one."""
    slots: list[tuple[str, str]] = []
    if settings.auth_jwt_secret:
        slots.append(("AUTH_JWT_SECRET", settings.auth_jwt_secret))
    slots += [
        (f"AUTH_JWT_SECRET_PREVIOUS[{i}]", v)
        for i, v in enumerate(settings.effective_jwt_secret_previous_list)
    ]
    if settings.token_hash_pepper:
        slots.append(("TOKEN_HASH_PEPPER", settings.token_hash_pepper))
    slots += [
        (f"TOKEN_HASH_PEPPER_PREVIOUS[{i}]", v)
        for i, v in enumerate(settings.token_hash_pepper_previous_list)
    ]
    # A blank key is "unset": the self-hosted compose file hands a Files-off
    # install an empty string, and with Files on `_validate_auth` already
    # refuses a blank one. Only a key someone actually set is judged here.
    files_key = (
        settings.files_content_signing_key.get_secret_value()
        if settings.files_content_signing_key is not None
        else ""
    )
    if files_key.strip():
        slots.append(("FILES_CONTENT_SIGNING_KEY", files_key))

    errors: list[str] = []
    for label, value in slots:
        # Padding adds bytes but no secret, so both checks look at what is left
        # once surrounding whitespace is gone; an all-whitespace value measures 0.
        core = value.strip()
        width = len(core.encode())
        if core in _PUBLISHED_DEV_SECRETS:
            errors.append(
                f"{label} is a local-dev value published in the Alkera repository; set a "
                f"random secret of at least {_PRODUCTION_SECRET_MIN_BYTES} bytes. "
                + _HOW_TO_GENERATE
            )
        elif width < _PRODUCTION_SECRET_MIN_BYTES:
            errors.append(
                f"{label} must be at least {_PRODUCTION_SECRET_MIN_BYTES} bytes, not counting "
                f"surrounding whitespace (got {width} bytes). " + _HOW_TO_GENERATE
            )
    return errors


def _validate_temporal_settings(settings: Settings) -> list[str]:
    """Production rules for the Temporal connection. Every worker, the backend and
    the health probe boot through these, so a misconfigured orchestrator fails loud
    at start instead of as a worker that polls nothing."""
    errors: list[str] = []
    host = settings.temporal_address.strip().lower()
    if not host:
        # A deploy that seeds the variable empty is not on the dev default, so
        # the loopback rule below would let the process boot and fail on its
        # first connect instead of here.
        errors.append("TEMPORAL_ADDRESS must be set (host:port of the Temporal server)")
    elif host.startswith(("localhost", "127.0.0.1", "[::1]")):
        errors.append(
            "TEMPORAL_ADDRESS must point at the production Temporal server "
            "(not the local dev default)"
        )
    if settings.temporal_api_key:
        if settings.temporal_tls is False:
            errors.append(
                "TEMPORAL_TLS=false with TEMPORAL_API_KEY set would send the key in "
                "cleartext — enable TLS or drop the key"
            )
        if "." not in settings.temporal_namespace:
            errors.append(
                "TEMPORAL_NAMESPACE must be the hosted form `<name>.<account-id>` when "
                "TEMPORAL_API_KEY is set (a key authenticates to a hosted namespace)"
            )
    if bool(settings.temporal_tls_client_cert) != bool(settings.temporal_tls_client_key):
        errors.append(
            "TEMPORAL_TLS_CLIENT_CERT and TEMPORAL_TLS_CLIENT_KEY must be set together "
            "(client mTLS needs both halves)"
        )
    for label, value, marker in (
        ("TEMPORAL_TLS_CLIENT_CERT", settings.temporal_tls_client_cert, "BEGIN CERTIFICATE"),
        ("TEMPORAL_TLS_CLIENT_KEY", settings.temporal_tls_client_key, "PRIVATE KEY"),
    ):
        if value and marker not in value and not _readable_file(value):
            errors.append(f"{label} must be inline PEM text or a readable PEM file path")
    return errors


#: Postgres refuses a NOTIFY payload above 8000 bytes; leave room for the framing.
NOTIFY_PAYLOAD_LIMIT_BYTES = 7900


def _validate_realtime_settings(settings: Settings) -> list[str]:
    """Every realtime knob individually sane, and the ones that constrain each
    other consistent. Returns every problem so one failed boot names them all."""
    errors: list[str] = []
    if not 0.05 <= settings.realtime_poll_interval_seconds <= 300:
        errors.append("REALTIME_POLL_INTERVAL_SECONDS must be between 0.05 and 300")
    keepalive = settings.realtime_sse_keepalive_seconds
    if not 1 <= keepalive <= 300:
        errors.append("REALTIME_SSE_KEEPALIVE_SECONDS must be between 1 and 300")
    if settings.realtime_sse_max_stream_seconds <= keepalive:
        errors.append(
            "REALTIME_SSE_MAX_STREAM_SECONDS must exceed REALTIME_SSE_KEEPALIVE_SECONDS "
            "(a keepalive slower than the stream deadline never fires)"
        )
    if settings.realtime_ws_max_session_seconds < 1:
        errors.append("REALTIME_WS_MAX_SESSION_SECONDS must be at least 1")
    for total_name, total, per_user_name, per_user, per_principal_name, per_principal in (
        (
            "REALTIME_SSE_MAX_STREAMS",
            settings.realtime_sse_max_streams,
            "REALTIME_SSE_MAX_STREAMS_PER_USER",
            settings.realtime_sse_max_streams_per_user,
            "REALTIME_SSE_MAX_STREAMS_PER_PRINCIPAL",
            settings.realtime_sse_max_streams_per_principal,
        ),
        (
            "REALTIME_WS_MAX_CONNECTIONS",
            settings.realtime_ws_max_connections,
            "REALTIME_WS_MAX_CONNECTIONS_PER_USER",
            settings.realtime_ws_max_connections_per_user,
            "REALTIME_WS_MAX_CONNECTIONS_PER_PRINCIPAL",
            settings.realtime_ws_max_connections_per_principal,
        ),
    ):
        if total < 1:
            errors.append(f"{total_name} must be at least 1")
        if per_user < 1:
            errors.append(f"{per_user_name} must be at least 1")
        if per_user > total:
            errors.append(f"{per_user_name} must not exceed {total_name}")
        if per_principal < 1:
            errors.append(f"{per_principal_name} must be at least 1")
    # The one place the websocket frame arithmetic is reconciled. uvicorn holds
    # up to the frame ceiling PER CONNECTION, so the process's peak receive-buffer
    # residency is that ceiling times the connection cap -- 1.03 GiB at today's
    # defaults, which nothing else in the repo states. A deployment that raises
    # the cap (or runs on a pod too small for the default) is told at boot rather
    # than by the OOM killer under load.
    if settings.realtime_ws_frame_budget_bytes < REALTIME_WS_MAX_FRAME_BYTES:
        errors.append(
            "REALTIME_WS_FRAME_BUDGET_BYTES must be at least "
            f"{REALTIME_WS_MAX_FRAME_BYTES} (one websocket frame; a smaller budget "
            "cannot admit a single connection)"
        )
    elif settings.realtime_ws_max_connections >= 1:
        peak = REALTIME_WS_MAX_FRAME_BYTES * settings.realtime_ws_max_connections
        if peak > settings.realtime_ws_frame_budget_bytes:
            affordable = settings.realtime_ws_frame_budget_bytes // REALTIME_WS_MAX_FRAME_BYTES
            errors.append(
                f"REALTIME_WS_MAX_CONNECTIONS ({settings.realtime_ws_max_connections}) x the "
                f"{REALTIME_WS_MAX_FRAME_BYTES}-byte websocket frame ceiling is {peak} bytes of "
                f"peak receive buffer, over REALTIME_WS_FRAME_BUDGET_BYTES "
                f"({settings.realtime_ws_frame_budget_bytes}) — lower the cap to "
                f"{affordable} or raise the budget and the container's memory with it"
            )
    if not 1 <= settings.realtime_ws_ticket_ttl_seconds <= 300:
        errors.append("REALTIME_WS_TICKET_TTL_SECONDS must be between 1 and 300")
    if settings.realtime_presence_ttl_seconds <= 2 * keepalive:
        errors.append(
            "REALTIME_PRESENCE_TTL_SECONDS must exceed twice REALTIME_SSE_KEEPALIVE_SECONDS "
            "(a peer must survive one missed keepalive)"
        )
    if not 1 <= settings.realtime_ephemeral_max_bytes <= NOTIFY_PAYLOAD_LIMIT_BYTES:
        errors.append(
            f"REALTIME_EPHEMERAL_MAX_BYTES must be between 1 and {NOTIFY_PAYLOAD_LIMIT_BYTES} "
            "(Postgres caps a NOTIFY payload at 8000 bytes)"
        )
    if settings.realtime_doc_max_bytes < 1024:
        errors.append("REALTIME_DOC_MAX_BYTES must be at least 1024")
    if settings.realtime_docs_max_per_org < 1:
        errors.append("REALTIME_DOCS_MAX_PER_ORG must be at least 1")
    if not 1 <= settings.realtime_crdt_workers <= 16:
        errors.append("REALTIME_CRDT_WORKERS must be between 1 and 16")
    if settings.realtime_crdt_worker_memory_mb < 256:
        errors.append("REALTIME_CRDT_WORKER_MEMORY_MB must be at least 256")
    if settings.realtime_crdt_cache_docs < 1:
        errors.append("REALTIME_CRDT_CACHE_DOCS must be at least 1")
    if settings.realtime_crdt_cache_bytes < 1024 * 1024:
        errors.append("REALTIME_CRDT_CACHE_BYTES must be at least 1048576")
    if settings.realtime_crdt_cache_bytes >= settings.realtime_crdt_worker_memory_mb * 1024 * 1024:
        errors.append(
            "REALTIME_CRDT_CACHE_BYTES must stay below REALTIME_CRDT_WORKER_MEMORY_MB "
            "(the cache lives inside the worker's address space)"
        )
    if not 100 <= settings.realtime_crdt_validate_timeout_ms <= 60_000:
        errors.append("REALTIME_CRDT_VALIDATE_TIMEOUT_MS must be between 100 and 60000")
    if (
        not settings.realtime_crdt_validate_timeout_ms
        <= (settings.realtime_crdt_load_timeout_ms)
        <= 300_000
    ):
        errors.append(
            "REALTIME_CRDT_LOAD_TIMEOUT_MS must be at least REALTIME_CRDT_VALIDATE_TIMEOUT_MS "
            "and at most 300000"
        )
    return errors


def _readable_file(path: str) -> bool:
    """A regular file the process can actually read — ``is_file()`` alone passes a
    mode-000 file and defers the failure to the first connect after deploy."""
    candidate = Path(path)
    return candidate.is_file() and os.access(candidate, os.R_OK)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor — import this, don't instantiate Settings directly."""
    return Settings()


settings: Settings = get_settings()
