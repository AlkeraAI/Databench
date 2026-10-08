"""The open gateway app. The product serves it through its composition root, which
installs its extensions first; an open install builds it here with none."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import cache
from types import FrameType
from typing import Annotated, Any

import httpx
from alkera_core.brand import product_name
from alkera_core.config import settings
from alkera_core.db.session import HealthSessionLocal
from alkera_core.egress import EgressPolicy
from alkera_core.entitlements import Feature, byok_active, has_feature, log_entitlements_status
from alkera_core.http import async_client, log_egress_status
from alkera_core.logging import configure_logging, get_logger
from alkera_core.observability.asgi import setup_observability
from alkera_core.readiness import Readiness, ReadinessChecks, database_check, probe
from alkera_core.schemas.system.health import LiveStatus, ReadyStatus
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from model_gateway import api
from model_gateway.extension_points import GATEWAY_ROUTERS
from model_gateway.meter import gateway_meter
from model_gateway.pipeline import begin_drain, drain_settlements, end_drain
from model_gateway.routability import NO_ROUTABLE_MODEL, has_routable_model

log = get_logger(__name__)

#: The signals a supervisor stops the process with. SIGINT is here too: a
#: developer's Ctrl-C should drain the same way a deploy does.
_STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)

#: What the gateway's readiness asks: the database it bills every request
#: against. A dependency the gateway adds later registers its own check here.
READINESS = ReadinessChecks()
READINESS.register(database_check)


def _assert_provider_config() -> None:
    """Fail fast (in non-local envs) when the gateway has no usable provider
    credentials.

    Without this, a gateway deployed with every provider key missing boots
    "healthy" and only fails when the first request reaches an unconfigured
    provider — a confusing 4xx to the end user instead of a clear boot error.

    Bedrock via an IAM role / instance profile leaves no env signal, so a
    deployment that relies *solely* on Bedrock-IAM opts out with
    ``GATEWAY_ASSUME_BEDROCK_IAM=true``. A BYOK-entitled deployment may hold NO
    env keys at all — its credentials live per-org in the DB, unknowable at
    boot, so it passes with its own log line.
    """
    if settings.is_local:
        return
    # Proxy mode holds no provider keys — Alkera's hosted gateway does. The token
    # is the credential, validated by the production config validator. A BYOK
    # entitlement is DORMANT under a proxy upstream (billed-through-Alkera):
    # warn so an operator who meant to flip modes sees it.
    if settings.gateway_proxy_mode:
        log.info("gateway.providers.proxy_upstream", url=settings.alkera_proxy_url)
        if has_feature(Feature.BYOK):
            log.warning(
                "gateway.byok.entitled_but_proxy_upstream",
                detail="the BYOK entitlement is dormant while GATEWAY_UPSTREAM=proxy — "
                "usage is metered + invoiced by Alkera",
            )
        return
    configured: list[str] = []
    if settings.anthropic_api_key:
        configured.append("anthropic")
    if settings.openai_api_key:
        configured.append("openai")
    if settings.aws_bearer_token_bedrock or settings.aws_access_key_id:
        configured.append("bedrock")
    if configured:
        log.info("gateway.providers.configured", providers=configured)
        if byok_active():
            log.info("gateway.providers.byok_enabled", env_fallback=configured)
        return
    if settings.gateway_assume_bedrock_iam:
        log.warning(
            "gateway.providers.iam_only",
            detail="no provider keys set; assuming a Bedrock IAM role is available",
        )
        return
    if byok_active():
        log.info(
            "gateway.providers.byok_db_credentials",
            detail="no instance provider keys; per-org BYOK credentials resolve from "
            "the database at request time",
        )
        return
    raise RuntimeError(
        "model-gateway has no provider credentials: set at least one of "
        "ANTHROPIC_API_KEY / OPENAI_API_KEY / AWS_BEARER_TOKEN_BEDROCK "
        "(or GATEWAY_ASSUME_BEDROCK_IAM=true if you rely solely on a Bedrock IAM role)."
    )


def install_drain_on_stop() -> Callable[[], None]:
    """Stop admitting new streams the moment the process is told to stop, and
    return the undo.

    The flip cannot wait for the lifespan's own shutdown phase: uvicorn closes
    the listening socket, lets every in-flight response run to
    ``--timeout-graceful-shutdown``, and only THEN sends the lifespan shutdown
    event — by which point there is nothing left to refuse. It has to happen on
    the signal.

    Chaining rather than replacing is deliberate. uvicorn installs its own
    handler with ``signal.signal`` ("so previous signal handlers can be restored
    later on"), so the handler it installed is exactly what ``getsignal`` hands
    back; ours sets the flag and then calls it, and the server shuts down as it
    always did. Registering through ``loop.add_signal_handler`` instead would
    silently displace uvicorn's handler and the process would never stop.
    """
    if threading.current_thread() is not threading.main_thread():
        # Signals are deliverable to the main thread only (a test server, or an
        # embedded run, gets no drain — it also gets no SIGTERM).
        return lambda: None

    previous: dict[int, Any] = {}

    def chain(prior: object) -> Callable[[int, FrameType | None], None]:
        def handler(signum: int, frame: FrameType | None) -> None:
            begin_drain()
            if callable(prior):
                prior(signum, frame)

        return handler

    for sig in _STOP_SIGNALS:
        try:
            prior = signal.getsignal(sig)
            signal.signal(sig, chain(prior))
        except (OSError, ValueError):  # pragma: no cover - platform-dependent
            continue
        previous[sig] = prior

    def restore() -> None:
        for sig, prior in previous.items():
            with contextlib.suppress(OSError, ValueError, TypeError):
                signal.signal(sig, prior)
        # The flag outlives the handlers only if the process is genuinely going
        # away; an app torn down and rebuilt in the same process (tests, an
        # embedded run) must admit streams again.
        end_drain()

    return restore


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    # A gateway that would verify sessions against a published dev secret never starts.
    settings.require_server_secrets()
    from model_gateway.estimate import warm_encodings

    # Load the token encodings here rather than on the first request that needs
    # one: tiktoken fetches the BPE ranks over the network on first use, with no
    # timeout, and the estimator that needs them runs on the admission path.
    # Bounded, off the event loop, and never fatal — an install that cannot
    # reach the ranks serves on the calibrated ratio instead.
    await asyncio.to_thread(warm_encodings)
    # Reclaim what a previous process left in flight before serving.
    meter = gateway_meter()
    log.info("gateway.meter.active", meter=meter.name)
    await meter.recover()
    restore_signals = install_drain_on_stop()
    try:
        yield
    finally:
        restore_signals()
        # A settle a hung-up client left running finishes before the loop that
        # runs it is torn down (see `drain_settlements`).
        await drain_settlements()
        for name in ("http_client", "byok_http_client"):
            client: httpx.AsyncClient | None = getattr(application.state, name, None)
            if client is not None:
                await client.aclose()


def create_app() -> FastAPI:
    configure_logging()

    # Refuse to boot misconfigured in prod/staging (clear error > first-request 4xx).
    _assert_provider_config()

    application = FastAPI(
        title=f"{product_name()} model gateway",
        version="0.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    # One pooled client for all upstream provider traffic. The read timeout bounds
    # how long a provider may go silent mid-response — one model step, never a
    # turn — and comes from a setting so a deployment whose models think for a
    # long time can raise it without a code change. Built via the shared factory
    # so a corporate proxy (HTTP(S)_PROXY env) and a custom CA (OUTBOUND_CA_BUNDLE)
    # apply to all egress.
    upstream_timeout = httpx.Timeout(
        connect=settings.gateway_upstream_connect_timeout_seconds,
        read=settings.gateway_upstream_read_timeout_seconds,
        write=settings.gateway_upstream_write_timeout_seconds,
        pool=settings.gateway_upstream_pool_timeout_seconds,
    )
    application.state.http_client = async_client(timeout=upstream_timeout)
    # A SECOND pooled client, for the one upstream URL the operator did not
    # choose: a BYOK `base_url` an org admin typed into the console. The gateway
    # streams that endpoint's response back to the caller verbatim, so an
    # unguarded one is a full-READ server-side request forgery — the whole
    # deployment's network, read by an ordinary employee role. This client vets
    # and PINS every request, so a name that resolves public at validation and
    # private at connect gains nothing, and the instance-metadata addresses are
    # refused whatever the deployment allows. Private ranges stay reachable on a
    # self-hosted install, which legitimately fronts an in-VPC inference proxy.
    application.state.byok_http_client = async_client(
        timeout=upstream_timeout,
        egress_policy=EgressPolicy.from_settings(allow_private=settings.is_self_hosted),
    )

    # Gated Sentry + per-request trace context / access logging + a catch-all
    # 500 handler. `full_error_envelope=False`: the gateway's 4xx bodies stay
    # provider-shaped (the LLM SDK clients parse them), so we do NOT reshape them.
    setup_observability(application, component="gateway", full_error_envelope=False)

    @application.get("/health/live", response_model=LiveStatus)
    async def live() -> LiveStatus:
        """Liveness — the process is up. Always 200."""
        return LiveStatus(status="ok")

    @application.get("/health/ready", response_model=ReadyStatus)
    async def ready(strict: Annotated[bool, Query()] = False) -> JSONResponse:
        """Readiness — the gateway can reach Postgres (it bills every request
        against it). 503 otherwise, so the load balancer drains this task —
        at once for a task that has never been ready, and past the grace
        window for one that has (see :mod:`alkera_core.readiness`).

        Asked on a connection of the probe's own, as the backend's is: a task
        whose request pool is drained is still able to reach the database and
        still sheds with retryable 503s, and pulling it would only move its
        traffic onto the tasks next in line. ``?strict=1`` answers 503 the
        moment the database is unreachable, with no grace window.

        A ready gateway with no routable model stays ready (a fresh install has
        no key yet); its body's ``detail`` says why no chat can start.
        """
        async with HealthSessionLocal() as db:
            answer = await probe(READINESS, db, byok=byok_active(), strict=strict)
        if answer.body.status == "ok":
            warning = await _routing_warning()
            if warning is not None:
                body = answer.body.model_copy(update={"detail": warning})
                answer = Readiness(answer.status_code, body)
        return answer.response()

    application.include_router(api.router)
    for extra in GATEWAY_ROUTERS.items():
        application.include_router(extra)

    log.info("gateway.startup", env=settings.app_env)
    log_entitlements_status("gateway")
    log_egress_status("gateway")
    return application


async def _routing_warning() -> str | None:
    """The readiness detail when no model can be routed, else None. A gateway
    with nothing to route stays ready (a fresh install has no key yet); the
    detail and the log say why no chat can start. A failed check warns nothing:
    the database answered the probe, and readiness is not this check's call."""
    try:
        async with HealthSessionLocal() as db:
            routable = await asyncio.wait_for(
                has_routable_model(db), timeout=settings.health_ready_timeout_seconds
            )
    except Exception as exc:
        log.warning("health.ready.routability_unknown", error_type=type(exc).__name__)
        return None
    if routable:
        return None
    log.warning("gateway.models.none_routable", detail=NO_ROUTABLE_MODEL)
    return NO_ROUTABLE_MODEL


@cache
def process_app() -> FastAPI:
    """This process's one gateway app, built on first call from the extensions
    installed by then. The product's composition root and a test
    suite both name it, so a process that reaches the app two ways holds a
    single instance."""
    return create_app()
