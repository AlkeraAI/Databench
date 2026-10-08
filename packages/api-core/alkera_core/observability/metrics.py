"""Prometheus metrics for the backend + gateway.

A tiny, low-cardinality RED surface (Rate / Errors / Duration) plus the default
process collectors (RSS, CPU, GC, open FDs) that ``prometheus_client`` registers.
Request instrumentation reuses the timing already done by
``RequestContextMiddleware`` — there is no second middleware pass — and is labelled
by ``component`` + ``method`` + ``status`` only (NOT path), so a UUID in a URL can't
explode the series count.

``/metrics`` is anonymous ONLY when ``APP_ENV=local``. In every other environment it
requires ``Authorization: Bearer <METRICS_AUTH_TOKEN>`` and answers 404 until that
token is set. The earlier "cluster-internal scrape, the reverse proxy doesn't forward
it" assumption holds for a self-hosted nginx deployment but is FALSE for the SaaS
topology: the ALB routes to the backend and gateway on the Host header alone — no path
condition, nothing in front of the ASGI app — so the endpoint is internet-reachable and
an anonymous one publishes request/error counts, latency and deploy times. Disable
entirely with ``METRICS_ENABLED=false``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

#: Set when the process is one of several serving the same task (uvicorn
#: ``--workers``). Each process then writes its values to files in this
#: directory and ``/metrics`` answers for all of them together; a scrape that
#: reached one process at random would otherwise see counters jump between
#: processes. prometheus_client reads it on import, and the directory has to
#: exist before the first value is created below.
MULTIPROC_DIR_ENV = "PROMETHEUS_MULTIPROC_DIR"
if os.environ.get(MULTIPROC_DIR_ENV):
    Path(os.environ[MULTIPROC_DIR_ENV]).mkdir(parents=True, exist_ok=True)

from prometheus_client import (  # noqa: E402 - the directory must exist first
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)

from alkera_core.config import settings  # noqa: E402

# Type-only: the realtime hub and listener record their gauges through this
# module from the worker too, and the worker image ships no web framework
# (starlette and FastAPI arrive with the backend). Only the ASGI endpoint at the
# bottom needs one, and it imports it when called.
if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

_REQUESTS = Counter(
    "alkera_http_requests_total",
    "Total HTTP requests handled.",
    ["component", "method", "status"],
)
_LATENCY = Histogram(
    "alkera_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["component", "method"],
)
# Realtime fan-out: one LISTEN connection per process feeding an in-process hub.
# Low cardinality by construction (a lane label with two values, no ids).
_REALTIME_LISTENER_CONNECTED = Gauge(
    "alkera_realtime_listener_connected",
    "1 while this process holds a live LISTEN connection to Postgres, else 0.",
    # Across processes: 0 when any of them has lost its listener.
    multiprocess_mode="livemin",
)
_REALTIME_EVENTS_PUBLISHED = Counter(
    "alkera_realtime_events_published_total",
    "Events offered to this process's subscribers, by lane.",
    ["lane"],
)
_REALTIME_HUB_SUBSCRIBERS = Gauge(
    "alkera_realtime_hub_subscribers",
    "Live in-process event subscriptions.",
    multiprocess_mode="livesum",
)
_REALTIME_HUB_OVERFLOW = Counter(
    "alkera_realtime_hub_overflow_total",
    "Subscriber queues that overflowed and were replaced by a reset marker.",
)
# Files: the object store behind the Files API. Set by the readiness probe (and
# the deployment-health check), so a store outage is visible as a flat 0 on this
# gauge for the whole time it lasts — an alert can fire on it without waiting for
# a write to fail. No labels: a process talks to exactly one store.
_FILES_STORE_AVAILABLE = Gauge(
    "files_store_available",
    "1 while this process's last object-store probe succeeded, else 0.",
    # The readiness probe reaches one process at random, and a process never
    # probed holds 0: the latest probe's answer, whichever process ran it.
    multiprocess_mode="livemostrecent",
)


# The process's SQLAlchemy pool. Every tenant's requests share it, so "how full
# is it" is the earliest sign that something is holding connections: the gauge
# climbs toward capacity minutes before the first checkout times out.
_DB_POOL_CHECKED_OUT = Gauge(
    "alkera_db_pool_checked_out",
    "Pooled database connections currently held by this process.",
    # A pool is exhausted per process: the fullest one is the one to watch.
    multiprocess_mode="livemax",
)
_DB_POOL_CAPACITY = Gauge(
    "alkera_db_pool_capacity",
    "The most pooled database connections this process may hold (size + overflow).",
    multiprocess_mode="livemax",
)
_DB_POOL_EXHAUSTED = Counter(
    "alkera_db_pool_exhausted_total",
    "Requests refused because no pooled database connection came free in time.",
)

# Authorization: decision rows that could not be written. A refusal still
# reaches its caller when its row cannot be written, so without this count a
# broken outbox would lose the audit trail of every refusal in silence. Labelled
# by what was being recorded (a single denial or a batch summary): two values.
_AUTHZ_DECISION_WRITE_FAILED = Counter(
    "authz_decision_write_failed_total",
    "Authorization decision rows that could not be written.",
    ["kind"],
)

# A lock taken out of the fixed order, or I/O awaited under a row lock, in a
# process that counts the break instead of raising it (alkera_core.db.locking).
# Flat at 0 on a healthy deployment; a climb names the rule in its label.
_DB_LOCK_RULE_VIOLATIONS = Counter(
    "alkera_db_lock_rule_violations_total",
    "Locks taken out of order, or calls awaited while a row lock was held.",
    ["rule"],
)

# Files: dedup-domain prefixes this database names but another deployment has
# marked as its own. A bucket restored from, or shared with, another stack makes
# every one of them uncollectable here, so the count is what tells an operator
# the two stacks are pointed at one bucket — before the storage bill does. It
# only ever climbs, and on a healthy deployment it is flat 0.
_FILES_FOREIGN_DOMAINS = Counter(
    "files_domain_marker_foreign_total",
    "Dedup domains found already marked by ANOTHER deployment.",
)


def record_listener_connected(connected: bool) -> None:
    """The listener's connection state flipped. No-op when metrics are disabled."""
    if not settings.metrics_enabled:
        return
    _REALTIME_LISTENER_CONNECTED.set(1 if connected else 0)


def record_event_published(lane: str) -> None:
    if not settings.metrics_enabled:
        return
    _REALTIME_EVENTS_PUBLISHED.labels(lane).inc()


def record_hub_subscribers(count: int) -> None:
    if not settings.metrics_enabled:
        return
    _REALTIME_HUB_SUBSCRIBERS.set(count)


def record_hub_overflow() -> None:
    if not settings.metrics_enabled:
        return
    _REALTIME_HUB_OVERFLOW.inc()


def record_files_store_available(available: bool) -> None:
    """The Files object-store probe answered (or did not). No-op when metrics
    are disabled."""
    if not settings.metrics_enabled:
        return
    _FILES_STORE_AVAILABLE.set(1 if available else 0)


def record_foreign_domain_marker() -> None:
    """One dedup domain's prefix turned out to be another deployment's. No-op
    when metrics are disabled."""
    if not settings.metrics_enabled:
        return
    _FILES_FOREIGN_DOMAINS.inc()


def record_authz_decision_write_failed(kind: str) -> None:
    """A decision row (``kind`` is ``deny`` or ``batch``) could not be written.
    No-op when metrics are disabled."""
    if not settings.metrics_enabled:
        return
    _AUTHZ_DECISION_WRITE_FAILED.labels(kind).inc()


def record_db_pool(*, checked_out: int, capacity: int) -> None:
    """How much of this process's database pool is in use. No-op when metrics
    are disabled."""
    if not settings.metrics_enabled:
        return
    _DB_POOL_CHECKED_OUT.set(checked_out)
    _DB_POOL_CAPACITY.set(capacity)


def record_db_pool_exhausted() -> None:
    """One request was refused because no pooled connection came free."""
    if not settings.metrics_enabled:
        return
    _DB_POOL_EXHAUSTED.inc()


def record_lock_rule_violation(rule: str) -> None:
    """One broken locking rule (``lock_order`` or ``io_under_lock``)."""
    if not settings.metrics_enabled:
        return
    _DB_LOCK_RULE_VIOLATIONS.labels(rule).inc()


def record_request(*, component: str, method: str, status: int, duration_seconds: float) -> None:
    """Record one finished request. ``component`` is bound to the app's middleware
    (NOT a process global) so backend + gateway can coexist in one process — e.g.
    the test suite — without clobbering each other's label. No-op when metrics
    are disabled, so the hot path costs nothing."""
    if not settings.metrics_enabled:
        return
    _REQUESTS.labels(component, method, str(status)).inc()
    _LATENCY.labels(component, method).observe(duration_seconds)


def exposition() -> bytes:
    """The Prometheus text exposition: this process's registry, or, when
    several processes share the task, all of them together (counters summed,
    gauges by their own rule). Per-process collectors (``process_*``, GC)
    cannot be aggregated and are not reported in that mode."""
    directory = os.environ.get(MULTIPROC_DIR_ENV)
    if not directory:
        return generate_latest()
    _forget_dead_processes(Path(directory))
    registry = CollectorRegistry()
    # prometheus_client's multiprocess module is untyped upstream.
    multiprocess.MultiProcessCollector(registry, path=directory)  # type: ignore[no-untyped-call]
    return generate_latest(registry)


def _forget_dead_processes(directory: Path) -> None:
    """Drop the live gauges of processes that are gone (a worker the
    supervisor replaced). Their counters stay: a total that went backwards
    would read as a reset."""
    from alkera_core.process import process_alive

    pids: set[int] = set()
    for path in directory.glob("gauge_live*_*.db"):
        try:
            pids.add(int(path.stem.rsplit("_", 1)[1]))
        except ValueError:
            continue
    for pid in pids:
        if not process_alive(pid):
            multiprocess.mark_process_dead(pid, path=str(directory))  # type: ignore[no-untyped-call]


async def metrics_endpoint(_request: Request) -> Response:
    """Prometheus text exposition (see :func:`exposition`)."""
    from starlette.responses import Response

    return Response(exposition(), media_type=CONTENT_TYPE_LATEST)
